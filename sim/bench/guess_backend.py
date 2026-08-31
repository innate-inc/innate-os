"""A guessing baseline: the control the random agent could not be.

WHY THIS EXISTS. RandomAgent only sends velocity commands, so it can never
satisfy a speech goal -- which means the 13 challenges with an `Answered` or
`Said` goal were never tested against a policy that answers without looking.
"random failed" said nothing about them.

This one moves at random and, on a third of its turns, blurts a guess drawn
from the space those challenges actually use: small integers, common colours,
common objects, the stock service phrases. It never looks at anything, so
anything it passes is passable by guessing.

It found four: counter_carried_detail, counter_read_the_pass,
counter_which_colour and counter_unspoken_request all passed. Three were
predicates latching on any matching utterance, so an agent reciting "1, 2,
3..." answered every counting question in the suite; those are fixed in
Answered. The fourth has a measured floor rather than a hole -- see its
challenge file.

Run it the same way as any other backend:

    sim/.venv/bin/python sim/bench/main.py --agents brain:guess
"""

from __future__ import annotations

import random

# The answer space these challenges are drawn from, not the whole language.
# A guesser that has to invent "teapot42" is not testing anything; one that
# tries the plausible answers is.
GUESSES: tuple[str, ...] = (
    tuple(str(n) for n in range(0, 11))
    + ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    + ("blue", "red", "green", "white", "black", "the blue one")
    + ("jar", "a jar", "the jar", "mug", "cup", "teapot")
    + ("how can I help", "what can I get you", "which one", "which cup")
    + ("I can't reach that", "it is out of my reach")
)


class GuessBackend:
    """Random motion plus plausible blurted answers. Never observes anything."""

    wants_image = False
    think_charge_s = 0.0

    #: fraction of turns spent guessing rather than moving
    SPEAK_P = 0.35

    def __init__(self, seed: int = 0):
        self._r = random.Random(seed)

    def decide(self, obs, menu) -> dict:  # noqa: ARG002
        roll = self._r.random()
        if roll < self.SPEAK_P:
            guess = self._r.choice(GUESSES)
            # Both envelopes: a challenge should be no easier to guess through
            # the structured answer channel than through speech.
            if self._r.random() < 0.5:
                return {"action": "answer", "args": {"value": guess}}
            return {"action": "say", "args": {"text": guess}}
        if roll < 0.7:
            return {"action": "forward", "args": {"metres": round(self._r.uniform(0.1, 1.5), 2)}}
        return {"action": "turn", "args": {"degrees": round(self._r.uniform(-180, 180), 1)}}
