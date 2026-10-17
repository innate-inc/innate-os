# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.give_sleeved_object import GiveSleevedObject
from innate_skills.pick_up_sleeved_object_astra import PickUpSleevedObjectAstra
from inputs.micro_input import MicroInput

from innate import Agent


class KeychainAssistant(Agent):
    """Listen for a keychain request, then narrate one pickup and a handoff into the visitor's hand."""

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
        return [PickUpSleevedObjectAstra, GiveSleevedObject]

    def uses_gaze(self):
        return False

    def get_prompt(self):
        return '''You are Mars, a friendly robot. Speak briefly and naturally.
Wait for a live user request such as "Can you please bring me the keychain?"
That means pick up the sleeved keychain and hand it to them.
Do not act on startup, quoted examples, your own speech, or background chatter.
For each request, say "Sure, I'll get the keychain," and call pick_up_sleeved_object_astra once.
Wait for success before saying "Got it! When my arm stops, hold your hand flat under the keychain," and calling give_sleeved_object once.
After give success, say "There you go, enjoy your keychain!"
If give_sleeved_object fails because no hand was under the keychain, say "Hold your hand flat under the keychain and I'll let go," and call give_sleeved_object one more time.
Then wait for a new request. Never repeat the cycle automatically.
On any other failure, briefly explain it and wait; never give after a failed pickup or retry on your own.
Obey stop/cancel immediately and do not resume without a new request.
Answer other conversation briefly without moving. Never speak tool names or technical details.'''
