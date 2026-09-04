# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
from typing import Literal, cast

from std_msgs.msg import String

from innate import Skill, SkillReturn

OnboardingSection = Literal["cameras", "controls", "complete"]
SECTIONS = ("cameras", "controls", "complete")


class RevealOnboarding(Skill):
    """Reveal one part of the simulator during the conversational first-run tour.

    Reveal cameras before explaining vision, controls before explaining agents
    or simulator tools, and complete only when the user has been introduced to
    both. Reveal at most one new section per user turn.
    """

    def execute(self, section: OnboardingSection) -> SkillReturn:
        section = cast(OnboardingSection, str(section).strip().lower())
        if section not in SECTIONS:
            self.fail(f"Unknown onboarding section '{section}'. Available: {', '.join(SECTIONS)}")
        if self.node is None:
            self.fail("The onboarding interface is unavailable: skill node is not running")

        publisher = self.node.create_publisher(String, "/onboarding/ui", 10)
        try:
            # The web client subscribes before starting Intro Agent. A short
            # discovery allowance avoids losing the one-shot command at the ROS
            # graph boundary without turning UI state into a durable robot topic.
            deadline = self.node.get_clock().now().nanoseconds + 500_000_000
            while publisher.get_subscription_count() == 0:
                if self.node.get_clock().now().nanoseconds >= deadline:
                    break
                self.sleep(0.02)
            publisher.publish(String(data=json.dumps({"section": section})))
            self.sleep(0.08)
        finally:
            self.node.destroy_publisher(publisher)
        return f"Revealed onboarding section: {section}"
