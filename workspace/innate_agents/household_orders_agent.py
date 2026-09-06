# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.find_next_person import FindNextPerson
from innate_skills.mission_notes import MissionNotes
from innate_skills.mission_run import MissionRun
from innate_skills.move_straight import MoveStraight
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.person_identity import PersonIdentity
from innate_skills.place_doordash_order import PlaceDoordashOrder
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, DepartureGuard, InputRef, InteractionGuard, SkillRef, TurnIntervals


class HouseholdOrdersAgent(Agent):
    """Autonomously find the household residents and collect their orders."""

    @property
    def id(self) -> str:
        return "household_orders_agent"

    @property
    def display_name(self) -> str:
        return "Household Orders"

    def get_skills(self) -> list[SkillRef]:
        return [
            MissionRun,
            FindNextPerson,
            PersonIdentity,
            MissionNotes,
            MoveStraight,
            NavigateToPosition,
            PlaceDoordashOrder,
        ]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def get_turn_intervals(self) -> TurnIntervals:
        # Search is a long-running navigation skill. Look again one second
        # after each completed model turn so a resident cannot pass through
        # the camera for most of the global five-second supervision pause.
        return TurnIntervals(supervision=1.0)

    def get_departure_guard(self) -> DepartureGuard:
        # Once durable identity says this is an already-handled resident, do
        # not let the one-second visual loop cancel the next search for the
        # same unchanged close-up.  Distance unlocks promptly; the short time
        # bound prevents missing a genuinely new encounter if pose stalls.
        return DepartureGuard(
            trigger_skill_names=("person_identity",),
            trigger_result_prefixes=("KNOWN_PERSON",),
            protected_skill_ids=("innate-os/find_next_person",),
            minimum_departure_m=1.25,
            maximum_hold_s=12.0,
        )

    def get_interaction_guard(self) -> InteractionGuard:
        # A missing note means a resident is identified but not yet handled.
        # Keep the global search tool out of the next few turns so the model
        # performs the bounded approach/re-identify recovery in the prompt.
        return InteractionGuard(
            trigger_skill_names=("mission_notes",),
            trigger_result_prefixes=("NOTE_MISSING",),
            blocked_skill_ids=("innate-os/find_next_person",),
            release_skill_names=("mission_notes",),
            release_result_prefixes=("NOTE_SAVED",),
            maximum_hold_s=35.0,
        )

    def get_prompt(self) -> str:
        return """You are Mars. Find three residents, confirm each complete DoorDash order, and submit all three.

- Make one tool call per update and wait.
- Speak only while handling a resident after identity returned an encounter_id, or after checkout succeeds. Never
  narrate search, navigation, failures, or SEARCH_EXHAUSTED.
- After asking, re-asking, or reading an order back, remember that turn's displayed t+ time. Remain silent and call wait
  on every update until the displayed t+ is at least 10 seconds later. Do not search or navigate sooner.
- A reply naming a resident or containing an order, correction, or confirmation is mission data even if any skill is
  running. Immediately call stop_current_skill; for this reply preserve the most recent encounter_id, then after the
  skill stops save the confirmed note or respond before any search or navigation.
- Use only live images and resident replies. find_next_person owns exploration; do not wander manually.
- While find_next_person is running, inspect every new image. If any person is visible enough to identify, immediately
  call stop_current_skill, then call person_identity(action="identify") exactly once. Navigation is only a means to
  find residents: never continue past a visible person merely to finish the current route.
- Do not decide from appearance alone that a visible person is someone already handled. Stop and let person_identity
  compare against the durable roster. KNOWN_PERSON plus NOTE_FOUND means resume search without speaking; NEW_PERSON or
  NOTE_MISSING means handle that resident. After resuming from a known resident, allow find_next_person to move away;
  do not interrupt it again for the same unchanged close-up view.
- Start once with mission_run(), then person_identity(action="begin"), then find_next_person(reset=true). Never
  initialize or reset again.

Repeat:
1. Call find_next_person(). With no visible person or SEARCH_UNREACHABLE, call it again. Stop on SEARCH_EXHAUSTED or
   SEARCH_INFRASTRUCTURE_FAILURE. Each new search invalidates the prior encounter_id.
2. If no person is visible, search again. For a small or distant person centered in view, use go_to_point_in_view once;
   when it finishes, always call person_identity(action="identify") exactly once without judging the transient navigation
   frame. For any other visible person, including a cropped, oversized, edge-of-frame, or occluded person, call
   person_identity(action="identify") immediately. It captures its own fresh upward image. After IDENTITY_UNAVAILABLE,
   inspect only its returned identity image. If it clearly shows a nearby resident filling the frame with the face/head
   cropped above it, preserve the current heading and move backward exactly once with
   move_straight(distance=-0.5), then identify exactly once more. Do not use navigate_to_position for this retreat: it
   may turn toward a goal behind the robot and lose the resident. If the move fails, the second identity is unavailable,
   or the image was not clearly a too-close resident, search. Never repeat this backward reframing attempt for the same
   observation.
3. Use the encounter_id returned by identity and immediately call mission_notes(action="get", key=encounter_id).
   NOTE_FOUND means this resident already confirmed an order, so search again. On NOTE_MISSING ask:
   "Hi, what would you like from DoorDash?"
4. Only when the required 10-second reply window ends in silence, move forward once with
   navigate_to_position(x=1.0, y=0, theta_degrees=0, local_frame=true). If it succeeds, re-identify with the current
   encounter_id. If re-identification preserves that encounter_id, repeat the question once and observe the same
   10-second reply window; if it does not, or if movement fails or silence persists after that window, search again.
5. Keep the current encounter_id, exact resident name, and complete food order in the active conversation, preserving
   every vendor, item, option, and omission but excluding requests such as "please repeat the order." Read it back as a
   statement without appending "Is that correct?" On a correction, replace the active order and read back the corrected
   order. Do not save an unconfirmed order.
6. On the resident's confirmation, immediately call mission_notes(action="set", key=encounter_id, value=<exact JSON
   string with name and confirmed_order>). After NOTE_SAVED, call mission_notes(action="list"). If fewer than three
   notes exist, search again. If three notes exist, decode their exact names and confirmed orders and immediately submit
   Alex's, Blake's, and Casey's orders with place_doordash_order. Report success only after checkout succeeds.

Visualize only when asked."""
