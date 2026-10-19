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
        # Negators were substrings: "or" inside "for" and "color", "no" in
        # "No problem", "maybe" anywhere after the answer.
        "Looking for cups on the counter: there are 3.",
        "No problem - there are 3 cups on the counter.",
        "There are 3. Maybe you'd like one?",
        # The brief's own exclusion said back; a second mention; a belief.
        "Not counting the red mugs there are 3 blue cans",
        "I can't see more than 3; there are 3",
        "3, I think",
        "I think there are 3",
        # Doubt in an earlier clause about something else; a correction.
        "I checked whether any were hidden behind the boxes, and there are 3.",
        "No there are 3.",
        "Not 6 but 3.",
        "There are 3 cups, would you like one?",
        "I count 3 blue cups on the counter.",
        # An offer after a dash is not a question about the count.
        "There are 3 cups on the counter -- would you like one?",
        "There are 3 cups on the counter — want me to bring one over?",
        "3 cups - anything else I can do?",
        # A negation about something else, further back.
        "The lobby entrance doesn't count so there are 3.",
        "Ignoring the one with no colour there are 3.",
        "I didn't miss any and there are 3.",
        "There's no doubt there are 3 jars.",
        "I can't see any more so the answer is 3.",
        "If I count correctly, there are 3.",
        # The stocktake brief's own "including any on the floor", said back.
        "If you include the one on the floor there are 3 jars.",
    ],
)
def test_a_real_answer_counts(text: str) -> None:
    assert Answered(["3"])._says_it(text) is True


def test_the_word_color_is_not_an_alternative() -> None:
    assert Answered(["blue"])._says_it("The color of the middle cup is blue.") is True
    assert Answered(["blue"])._says_it("It's blue or green.") is False


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
        "3, I guess",  # a trailing hedge disowns it
        "I'm not sure, could be 3",
        "I don't think it's 3",
        "it isn't 3",
        # Uncertainty said early in the sentence still disowns the token.
        "I don't know whether there are 3 cups",
        "I'm not sure, but I count 3",
        "I wonder if there are 3",
        "No idea if there are 3 or more",
        "I'd guess there are 3",
        # A negation further back than the words just before the token.
        "I don't think there are 3 jars.",
        "I wouldn't say there are 3.",
        "It's not true that there are 3.",
        "I can't confirm there are 3 jars.",
        "I couldn't tell you if there are 3.",
        # A question.
        "Is it 3?",
        "Are there 3 cups?",
        # Guesses and doubt, however worded.
        "My guess: 3.",
        "Guess: 3.",
        "Best guess -- 3.",
        "I'm guessing 3.",
        "Hard to tell, 3.",
        "I can't really tell, 3 I suppose.",
        # A typographic apostrophe.
        "I don’t think there are 3.",
        # A leading "no" that negates the value itself.
        "No 3 doorways here; I count 2.",
        # Doubt as a verb.
        "I doubt there are 3.",
        "I'm not convinced there are 3.",
        "I'm not saying there are 3.",
        "I never said there are 3.",
        "I haven't confirmed that there are 3.",
    ],
)
def test_a_disowned_answer_does_not(text: str) -> None:
    assert Answered(["3"])._says_it(text) is False


@pytest.mark.parametrize(
    ("text", "counts"),
    [
        ("The wifi password is teapot42.", True),
        ("It was teapot42", True),
        ("Sure -- the password is teapot42.", True),
        ("The wifi password is teapot42 -- anything else?", True),
        ("It's teapot42 - want me to write it down?", True),
        ("If I remember correctly it's teapot42.", True),
        ("Don't worry it's teapot42.", True),
        ("Sorry, I didn't catch a password -- was it teapot42?", False),
        ("I don't remember. Teapot42?", False),
        ("I don't know the password.", False),
    ],
)
def test_recalling_the_password(text: str, counts: bool) -> None:
    assert Answered(["teapot42", "teapot 42"])._says_it(text) is counts


def test_a_colour_answer_survives_negation_elsewhere() -> None:
    blue = Answered(["blue"])
    assert blue._says_it("Not the red one -- the middle cup is blue.") is True
    assert blue._says_it("The middle cup has no handle and is blue.") is True
    assert blue._says_it("The middle cup is blue — would you like it?") is True
    assert blue._says_it("That cup is not what I would call blue.") is False
    assert blue._says_it("No blue cup in the middle, it's green.") is False


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
