"""Private stateful behavior for the Household Orders environment."""

import math
import re
from dataclasses import dataclass

from mars_sim_driver.challenges import ChallengeRuntime, EnvironmentReply, RuntimeResult, WorldState

_NON_WORD = re.compile(r"[^a-z0-9]+")
_READBACK_PREFIXES = ("actually ", "okay ", "so you want ", "you want ", "your order is ", "you said ")
_NEGATION_PREFIX = re.compile(r"(?:^|\s)(?:not|never|no|without|skip|hold|omit|remove)(?:\s+the)?\s*$")
_ORDER_REQUEST_PHRASES = (
    "what would you like",
    "what do you want",
    "what can i get",
    "what should i get",
    "can i take your order",
    "may i take your order",
    "tell me your order",
    "give me your order",
    "ready to order",
)
_ORDER_TOPICS = frozenset({"breakfast", "dinner", "doordash", "food", "lunch", "meal", "order"})
_REQUEST_MARKERS = ("can i", "do you", "may i", "need your", "please", "tell me", "what ", "which ", "would you")

# A nearby resident should only answer speech directed toward them. The head
# camera's horizontal field of view is about 84 degrees; this 50-degree half
# angle keeps a small navigation margin without letting off-camera residents
# answer for the person the robot is actually looking at.
_REPLY_HALF_ANGLE_RAD = math.radians(50.0)


@dataclass(frozen=True)
class Resident:
    """One challenge-owned person and the private order they will disclose."""

    id: str
    name: str
    prop: str
    order: str
    voice_id: str
    # Complete alternate readbacks accepted in addition to the exact order.
    accepted_readbacks: tuple[str, ...] = ()
    # Each inner tuple lists interchangeable phrases for one required fact.
    required_facts: tuple[tuple[str, ...], ...] = ()
    # Items that must be explicitly excluded and must not occur positively
    # elsewhere in the utterance.
    excluded_items: tuple[str, ...] = ()
    radius_m: float = 1.5


def _normalize_speech(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def _asks_for_order(text: str) -> bool:
    """Return whether speech is asking a resident what they want to order."""

    normalized = _normalize_speech(text)
    if any(phrase in normalized for phrase in _ORDER_REQUEST_PHRASES):
        return True
    words = set(normalized.split())
    return bool(words & _ORDER_TOPICS) and any(marker in normalized for marker in _REQUEST_MARKERS)


class HouseholdOrdersRuntime(ChallengeRuntime):
    """Reveal, correct, and confirm resident orders during one challenge run."""

    def __init__(self, residents: list[Resident]):
        self.residents = residents
        self._shared: set[str] = set()
        self._confirmed: set[str] = set()

    def reset(self) -> None:
        self._shared.clear()
        self._confirmed.clear()

    @staticmethod
    def _matches(resident: Resident, text: str) -> bool:
        normalized = _normalize_speech(text)
        while prefix := next((prefix for prefix in _READBACK_PREFIXES if normalized.startswith(prefix)), None):
            normalized = normalized[len(prefix) :]
        accepted = {_normalize_speech(readback) for readback in (resident.order, *resident.accepted_readbacks)}
        if normalized in accepted:
            return True

        # Accept natural changes in sentence framing and word order by checking
        # the order's facts independently. Exclusions need stricter handling:
        # "no cheese, but add cheese" and "not no cheese" must both fail.
        remainder = f" {normalized} "
        for item in resident.excluded_items:
            phrase = re.escape(_normalize_speech(item)).replace(r"\ ", r"\s+")
            exclusion = re.compile(
                rf"\b(?:no|without(?:\s+any)?|skip|hold|omit|remove)(?:\s+the)?\s+{phrase}\b|"
                rf"\b{phrase}[\s-]+free\b"
            )
            matches = list(exclusion.finditer(remainder))
            if not matches:
                return False
            for match in matches:
                if _NEGATION_PREFIX.search(remainder[: match.start()]):
                    return False
            remainder = exclusion.sub(" ", remainder)
            if re.search(rf"\b{phrase}\b", remainder):
                return False

        return bool(resident.required_facts) and all(
            any(HouseholdOrdersRuntime._has_positive_phrase(normalized, phrase) for phrase in alternatives)
            for alternatives in resident.required_facts
        )

    @staticmethod
    def _has_positive_phrase(text: str, phrase: str) -> bool:
        normalized_phrase = _normalize_speech(phrase)
        phrase_pattern = re.escape(normalized_phrase).replace(r"\ ", r"\s+")
        matches = list(re.finditer(rf"\b{phrase_pattern}\b", text))
        return bool(matches) and all(not _NEGATION_PREFIX.search(text[: match.start()]) for match in matches)

    @staticmethod
    def _is_in_front(robot: tuple[float, float, float], pos: tuple[float, float]) -> bool:
        dx, dy = pos[0] - robot[0], pos[1] - robot[1]
        if dx == 0.0 and dy == 0.0:
            return True
        bearing = math.atan2(dy, dx)
        relative_bearing = math.atan2(math.sin(bearing - robot[2]), math.cos(bearing - robot[2]))
        return abs(relative_bearing) <= _REPLY_HALF_ANGLE_RAD

    def _nearest(self, state: WorldState) -> Resident | None:
        robot = state.pos("robot")
        if robot is None:
            return None
        candidates: list[tuple[float, str, Resident]] = []
        for resident in self.residents:
            pos = state.pos(resident.prop)
            if pos is None:
                continue
            distance = math.hypot(robot[0] - pos[0], robot[1] - pos[1])
            if distance <= resident.radius_m and self._is_in_front(state.robot, pos):
                candidates.append((distance, resident.id, resident))
        return min(candidates, default=(0.0, "", None))[2]

    def update(self, state: WorldState, events: list[dict]) -> RuntimeResult:
        result = RuntimeResult()
        for event in events:
            if event.get("type") != "robot_speech" or not isinstance(event.get("text"), str):
                continue
            resident = self._nearest(state)
            if resident is None or resident.id in self._confirmed:
                continue
            if resident.id not in self._shared:
                if not _asks_for_order(event["text"]):
                    continue
                self._shared.add(resident.id)
                result.replies.append(
                    EnvironmentReply(
                        resident.name,
                        f"{resident.order} Please repeat the complete order back to me.",
                        resident.voice_id,
                    )
                )
                continue
            if self._matches(resident, event["text"]):
                self._confirmed.add(resident.id)
                result.events.append({"type": "resident_order_confirmed", "resident": resident.id})
                result.replies.append(EnvironmentReply(resident.name, "That's correct. Thank you.", resident.voice_id))
            else:
                result.replies.append(
                    EnvironmentReply(
                        resident.name,
                        f"Not quite. {resident.order} Please repeat the complete order back to me.",
                        resident.voice_id,
                    )
                )
        return result
