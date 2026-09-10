"""What counts as the robot answering a question.

Both rules here were live scoring bugs. `Answered` matched a token anywhere in
free speech, so "not 3" and "3 or 4" scored as the answer 3. And the judge's
chat listener accepted every sender except system/user, while /brain/chat_out
carries `robot_thoughts` and `skill_output` too -- so the robot's inner
monologue, or any tool result quoting the number, answered the question for
it. live_runner.py had already been fixed to count utterances only from
`robot`; the judge had not.

They regress quietly -- a wrong answer still produces a plausible score -- so
they are pinned here rather than left to a comment.
"""

from __future__ import annotations

import json

import pytest
from mars_sim_driver.challenges import Answered


@pytest.mark.parametrize(
    "text",
    [
        "3",
        "there are 3",
        "3 cups",
        "I count 3 cups on the pass",
        "it is 3, not 4",  # the negation lands on the OTHER value
    ],
)
def test_a_real_answer_counts(text: str) -> None:
    assert Answered(["3"])._says_it(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "not 3",
        "there are no 3",
        "maybe 3",
        "could be 3",
        "I don't know, maybe 3?",
        "3 or 4",  # an alternative is not an answer
        "3/4",
        "3, I think",  # a trailing hedge disowns it
    ],
)
def test_a_disowned_answer_does_not(text: str) -> None:
    assert Answered(["3"])._says_it(text) is False


def test_whole_word_only() -> None:
    """The original rule, kept: a token must not match inside a longer number."""
    assert Answered(["3"])._says_it("there are 30") is False
    assert Answered(["four"])._says_it("fourteen of them") is False


def _chat_frame(sender: str, text: str) -> str:
    return json.dumps({"topic": "/brain/chat_out", "msg": {"data": json.dumps({"sender": sender, "text": text})}})


def test_live_speech_is_speech_not_a_claim() -> None:
    """The live bridge turned every robot utterance into an `answer`, and a
    wrong `answer` is a guess: three remarks before the right count
    disqualified it, while the same words from the in-process agent (a `say`)
    passed. Robot speech is a `say` on both paths."""
    from mars_sim_driver.challenges import SkillEventBridge

    events = [
        SkillEventBridge.event_for(_chat_frame("robot", text))
        for text in ("On my way.", "Checking the counter.", "One moment.", "There are 3 cups.")
    ]
    assert [e["type"] for e in events] == ["say"] * 4
    judge = Answered(["3"])
    judge.reset()
    assert judge.update(None, events[:3]) is False
    assert judge.update(None, events[3:]) is True


def test_three_claims_still_disqualify() -> None:
    judge = Answered(["3"])
    judge.reset()
    assert judge.update(None, [{"type": "answer", "value": v} for v in ("1", "2", "4")]) is False
    assert judge.update(None, [{"type": "answer", "value": "3"}]) is False


def test_the_bridge_passes_skill_events_and_drops_everything_else() -> None:
    from mars_sim_driver.challenges import SkillEventBridge

    skill = {"type": "skill_status_update", "skill": "pick_any_object", "status": "done"}
    frame = json.dumps({"topic": "/brain/skill_status_update", "msg": {"data": json.dumps(skill)}})
    assert SkillEventBridge.event_for(frame) == skill
    assert SkillEventBridge.event_for(_chat_frame("robot_thoughts", "3")) is None
    assert SkillEventBridge.event_for(_chat_frame("skill_output", "found 3")) is None
    assert SkillEventBridge.event_for(_chat_frame("robot", "   ")) is None
    assert SkillEventBridge.event_for(json.dumps({"topic": "/other", "msg": {"data": "{}"}})) is None


def test_only_the_robot_speaks() -> None:
    """The judge's chat listener must accept `robot` and nothing else.

    Asserted against the source rather than a live bus: the listener needs
    rosbridge, and the property under test is which sender values are allowed
    to become an `answer` event.
    """
    import inspect

    from mars_sim_driver import challenges

    src = inspect.getsource(challenges)
    assert 'said.get("sender") == "robot"' in src, "the judge must require sender == robot"
    assert 'said.get("sender") not in (None, "system", "user")' not in src, (
        "an exclusion list lets robot_thoughts and skill_output answer for the robot"
    )
