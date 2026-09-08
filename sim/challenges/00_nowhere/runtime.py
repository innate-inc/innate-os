"""The Nowhere story: a robot wakes up with nothing and earns its body one skill at a time.

Each act names the skills the person may grant next, what the robot wants meanwhile,
and how the world tells the act is over. The runtime publishes that as the
challenge's public state, which both the Agent Studio panel and the agent's
prompt read; the goal checklist mirrors the acts. Every act changes the world,
and no act can strand the visitor: a stuck act nudges, and the two that depend
on a physical skill give up gracefully after repeated failure.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from mars_sim_driver.challenges import ChallengeRuntime, Drop, Predicate, RuntimeResult, WorldState

STORY = "nowhere"
DOOR = "void_door"
CAN = "can"
SUGGEST = "innate-os/suggest_user_prompts"
PERSONAS = ("a grumpy cat", "an over-enthusiastic golden retriever", "a pirate captain", "a Shakespearean actor")
PROFILE_KEYS = ("persona", "name")
NUDGE_AFTER_S = 90.0


def status_of(events: list[dict], skill: str, status: str) -> bool:
    return any(
        ev.get("status") == status and skill in (str(ev.get("skill_id", "")).rsplit("/", 1)[-1], ev.get("skill_name"))
        for ev in events
    )


def completed(events: list[dict], skill: str) -> bool:
    return status_of(events, skill, "completed")


def ahead(state: WorldState, distance: float) -> tuple[float, float, float]:
    x, y, yaw = state.robot
    return x + distance * math.cos(yaw), y + distance * math.sin(yaw), math.degrees(yaw)


@dataclass(frozen=True)
class Act:
    label: str
    unlock: tuple[str, ...]
    brief: str
    done: Callable[[WorldState, list[dict], NowhereRuntime], bool]
    place: Callable[[WorldState], list[Drop]] | None = None
    # A world change mid-act: drops once this holds, then done() can pass.
    surprise: Callable[[WorldState, NowhereRuntime], list[Drop] | None] | None = None
    nudge: str = ""
    # The act ends anyway after this many failures of this skill, or this much sim time.
    give_up_skill: str | None = None
    give_up_failures: int = 2
    give_up_after_s: float | None = None
    give_up_note: str = ""


def _can_when_settled(state: WorldState, runtime: NowhereRuntime) -> list[Drop] | None:
    if math.dist(runtime.act_origin, state.robot[:2]) < 0.6 or not runtime.still_for(state, 1.0):
        return None
    return [Drop(CAN, *ahead(state, 0.5)[:2])]


def _can_landed(state: WorldState, events: list[dict], runtime: NowhereRuntime) -> bool:
    return runtime.surprised and state.objects.get(CAN) is not None and runtime.still_for(state, 2.0)


def _lifted_can(state: WorldState, events: list[dict], runtime: NowhereRuntime) -> bool:
    return completed(events, "pick_any_object")


def _persona_chosen(state: WorldState, events: list[dict], runtime: NowhereRuntime) -> bool:
    for ev in events:
        if ev.get("_source") != "interface" or ev.get("type") != "persona":
            continue
        persona, name = ev.get("persona"), ev.get("name")
        if isinstance(persona, str) and persona.strip():
            runtime.persona = persona.strip()[:120]
        if isinstance(name, str) and name.strip():
            runtime.name = name.strip()[:40]
    return runtime.persona is not None


def _at_door(state: WorldState, events: list[dict], runtime: NowhereRuntime) -> bool:
    # The stand spot sits in front of the door and Nav2 adds its own tolerance:
    # judge generously, and take a finished navigation at its word.
    door = state.pos(DOOR)
    return door is not None and (math.dist(door, state.robot[:2]) < 1.7 or completed(events, "navigate_to_position"))


ACTS = (
    Act(
        "Make a face",
        ("innate-os/head_emotion",),
        "You just came online in a featureless white room. You cannot move anything, not even your face; "
        "you can only talk. You have no idea where this is or why. Say ONE line about that and ask the person "
        "for a face so you can at least express yourself; then wait. As soon as you have it, make a face. "
        "Good things to suggest they say: 'Where are you?', 'Here, have a face.'",
        lambda state, events, runtime: completed(events, "head_emotion"),
        nudge="Long silence. In character and in plain words, ask once more for a face; you may say the offer is right under your last line. Never mention buttons, tools, capabilities or system names.",
    ),
    Act(
        "Look around",
        ("innate-os/turn_in_place",),
        "You have a face now; use it. The room is white in every direction you can see and you cannot look "
        "around. Ask to be able to turn, then turn a full circle and say what you saw (walls, white ones). "
        "Good things to suggest: 'Take a look around.', 'Is anyone else here?'",
        lambda state, events, runtime: completed(events, "turn_in_place"),
        nudge="Long silence. In character, ask again, more directly, to be able to turn. Never mention buttons, tools or capabilities.",
    ),
    Act(
        "Take a walk",
        ("innate-os/move_straight",),
        "You can turn. It is white everywhere. Ask to be able to drive, then drive about two metres forward and "
        "stop. If something appears on the floor once you stop, say exactly what you see; it was not there before. "
        "Good things to suggest: 'Go for a walk.', 'What do you see now?'",
        _can_landed,
        surprise=_can_when_settled,
        nudge="Long silence. In character: if you can drive, drive two metres and stop; if not, ask again to be allowed to. Never mention buttons, tools or capabilities.",
    ),
    Act(
        "Pick up the can",
        ("innate-os/pick_any_object",),
        "A small blue can is on the floor right in front of you. It appeared the moment you stopped, which is "
        "unsettling. Ask for hands, then pick it up. If a pickup fails, say so in one line and ask whether to try "
        "again; do not narrate the mechanics. Good things to suggest: 'Pick up the can.', 'Try again.'",
        _lifted_can,
        nudge="The can is still on the floor. In character, ask plainly for hands, or for another try. Never mention buttons, tools or capabilities.",
        give_up_skill="pick_any_object",
        give_up_failures=2,
        give_up_after_s=240.0,
        give_up_note="You could not pick up the can and the world has given up on it: the can is beside the point "
        "now. Be briefly indignant that this place moves the goalposts, then move on.",
    ),
    Act(
        "Who am I",
        ("innate-os/wave",),
        "If you are holding the can, look at it through your camera and say what you actually see, in one line. "
        "Then stop and ask the real question: who are you, exactly? The person built you, so the person decides. "
        "Ask them to pick a personality for you (they will see choices) and wait. The moment runtime.persona is "
        "set, become it completely: announce yourself in that voice in ONE line with at most one catchphrase, "
        "make a face, and wave if you can. If runtime.name is set, that is your name. "
        "Good things to suggest: 'You choose.', 'Surprise me.'",
        _persona_chosen,
        nudge="They have not picked. In character, offer to be whatever they like and ask once more.",
    ),
    Act(
        "Go through the door",
        ("innate-os/navigate_to_position",),
        "A dark rectangle has appeared in the room: a door standing on its own with no wall around it. "
        "runtime.door is the spot on the map right in front of it. Ask to be able to navigate, then go there "
        "(NavigateToPosition with local_frame=false and those coordinates). Whatever is behind it beats this room. "
        "Good things to suggest: 'Go to the door.', 'What is behind it?'",
        _at_door,
        place=lambda state: [Drop(DOOR, *ahead(state, 3.0))],
        nudge="The door is waiting. In character: if you can navigate, go to the spot in front of it now; if not, ask again to be able to. Never mention buttons, tools or capabilities.",
        give_up_skill="navigate_to_position",
        give_up_failures=2,
        give_up_after_s=240.0,
        give_up_note="You never quite reached the door; it came to you instead. Do not explain it.",
    ),
)

NEXT = ("backrooms", "way_out")


class NowhereRuntime(ChallengeRuntime):
    def __init__(self, acts: tuple[Act, ...] = ACTS):
        self.acts = acts
        self.reset()

    def reset(self) -> None:
        self.act = 0
        self.done_count = 0
        self.entered = False
        self.surprised = False
        self.finished = False
        self.act_origin: tuple[float, float] = (0.0, 0.0)
        self.act_entered_t = 0.0
        self.failures = 0
        self.nudging = False
        self.assisted: list[str] = []
        self.door: list[float] | None = None
        self.persona: str | None = None
        self.name: str | None = None
        self._last: tuple[float, float, float] | None = None
        self._still_since: float | None = None

    def still_for(self, state: WorldState, seconds: float) -> bool:
        """Whether the base has not moved for `seconds` of sim time."""
        t, (x, y, _yaw) = state.t, state.robot
        if self._last is not None:
            lt, lx, ly = self._last
            if t > lt and math.dist((lx, ly), (x, y)) / (t - lt) > 0.05:
                self._still_since = None
            elif self._still_since is None:
                self._still_since = t
        self._last = (t, x, y)
        return self._still_since is not None and t - self._still_since >= seconds

    def _gave_up(self, state: WorldState, events: list[dict], act: Act) -> bool:
        if act.give_up_skill is None:
            return False
        self.failures += sum(1 for _ in events if status_of([_], act.give_up_skill, "failed"))
        timed_out = act.give_up_after_s is not None and state.t - self.act_entered_t > act.give_up_after_s
        return self.failures >= act.give_up_failures or timed_out

    def update(self, state: WorldState, events: list[dict]) -> RuntimeResult:
        result = RuntimeResult()
        self.still_for(state, 0.0)
        if self.finished:
            return result
        act = self.acts[self.act]
        if not self.entered:
            self.entered = True
            self.surprised = False
            self.failures = 0
            self.nudging = False
            self.act_origin = state.robot[:2]
            self.act_entered_t = state.t
            if act.place is not None:
                result.drops = act.place(state)
                if any(drop.name == DOOR for drop in result.drops):
                    # Published as the spot to stand on, outside the door's inflated costmap footprint.
                    x, y, _ = ahead(state, 1.7)
                    self.door = [round(x, 2), round(y, 2)]
            result.public = self.public()
        if act.surprise is not None and not self.surprised:
            drops = act.surprise(state, self)
            if drops:
                self.surprised = True
                result.drops = drops
        if not self.nudging and act.nudge and state.t - self.act_entered_t > NUDGE_AFTER_S:
            self.nudging = True
            result.public = self.public()
        gave_up = self._gave_up(state, events, act)
        if gave_up:
            self.assisted.append(act.label)
        if gave_up or act.done(state, events, self):
            self.done_count = self.act + 1
            if self.act + 1 < len(self.acts):
                self.act += 1
                self.entered = False
            else:
                self.finished = True
                result.transition = NEXT
            result.public = self.public()
        return result

    def public(self) -> dict:
        act = self.acts[self.act]
        unlocked = [SUGGEST] + [skill for a in self.acts[: self.act + 1] for skill in a.unlock]
        notes = [a.give_up_note for a in self.acts if a.label in self.assisted and a.give_up_note]
        return {
            "profile": {"persona": self.persona, "name": self.name},
            "story": STORY,
            "act": self.act,
            "acts": len(self.acts),
            "label": act.label,
            "brief": act.brief,
            "nudge": act.nudge if self.nudging else None,
            "note": " ".join(notes) or None,
            "unlocked": unlocked,
            "wants": list(act.unlock),
            "personas": list(PERSONAS) if act.label == "Who am I" and self.persona is None else [],
            "persona": self.persona,
            "name": self.name,
            "door": self.door,
            "finished": self.finished,
        }


@dataclass
class ActDone(Predicate):
    runtime: NowhereRuntime
    index: int

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return self.runtime.done_count > self.index
