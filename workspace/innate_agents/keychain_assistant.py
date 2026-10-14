# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Agent
from inputs.micro_input import MicroInput
from innate_skills.pick_up_sleeved_object_astra import PickUpSleevedObjectAstra
from innate_skills.drop_sleeved_object_in_box import DropSleevedObjectInBox


class KeychainAssistant(Agent):
    """Listen for a keychain request, then narrate one pickup and box delivery."""

    model = 'google:gemini-3.8-flash'
    model_extra_body = '{}'  # Do not inherit Qwen-specific request options.
    minimal_system_prompt = True
    wait_for_skill_completion = True
    history_max_entries = 16
    history_max_image_turns = 0

    @property
    def id(self):
        return 'keychain_assistant'

    @property
    def display_name(self):
        return 'Keychain Assistant'

    def get_inputs(self):
        return [MicroInput]

    def get_skills(self):
        return [PickUpSleevedObjectAstra, DropSleevedObjectInBox]

    def uses_gaze(self):
        return False

    def get_prompt(self):
        return '''You are Mars, a friendly robot. Speak briefly and naturally.
Wait for a live user request such as "Can you please bring me the keychain?"
That means pick up the sleeved keychain and put it in the marker box.
Do not act on startup, quoted examples, your own speech, or background chatter.
For each request, say "Sure, I'll get the keychain," and call pick_up_sleeved_object_astra once.
Wait for success before saying "Got it. I'll put it in the box," and calling drop_sleeved_object_in_box once.
After drop success, say "There you go—I've released it over the box." Do not claim visual confirmation of landing.
Then wait for a new request. Never repeat the cycle automatically.
On any failure, briefly explain it and wait; never drop after failed pickup or retry on your own.
Obey stop/cancel immediately and do not resume without a new request.
Answer other conversation briefly without moving. Never speak tool names or technical details.'''
