"""The suggestion tool accepts bounded requests and never emits speech/actions."""

import sys
import unittest
from pathlib import Path

from brain_client.skills.types import SkillFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "workspace"))
from innate_skills.suggest_user_prompts import SuggestUserPrompts  # noqa: E402


class SuggestUserPromptsTests(unittest.TestCase):
    def test_requests_and_clear_succeed_but_malformed_input_fails(self):
        skill = SuggestUserPrompts.__new__(SuggestUserPrompts)
        for prompts in [[], ["Try picking it up again"], ["Pick it up", "Put it in the box", "What can you see?"]]:
            self.assertIsNone(skill.execute(prompts))
        for prompts in [None, "Pick it up", [""], [" "], [1], ["x" * 161], ["Pick it up"] * 4]:
            with self.subTest(prompts=prompts), self.assertRaises(SkillFailed):
                skill.execute(prompts)
