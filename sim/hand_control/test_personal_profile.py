import copy
import json
import tempfile
import unittest
from pathlib import Path

from personal_profile import load_profile


class PersonalProfileTests(unittest.TestCase):
    def profile(self):
        return {
            "version": 1,
            "reference": [1, 0, 0, 0, 1, 0, 0, 0, 1],
            "rotation": {
                "matrix": [[1, 0, 0], [0, 1, 0]],
                "response": [{"deadzone": 0.01, "negative": 1, "positive": 1}] * 2,
            },
            "position": {"matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "compensation": [[0] * 5] * 3},
            "grip": {"knots": [0.3, 1, 1.5], "neutral": 0.55},
            "workspace": {"center": [0.35, -0.05, 0.13], "span": [0.04, 0.1, 0.13], "angles": [0] * 5, "grip": 0.55},
            "floor": {
                "version": 1,
                "width": 0.65,
                "scales": [1] * 8,
                "centers": [[0] * 8],
                "coefficients": [[0] * 5],
                "grip": {
                    "pitch": [0, 0.78, 1.4],
                    "closed": [0.2, 0.3, 0.3],
                    "neutral": [1.4, 2, 2.1],
                    "open": [2.3, 2.4, 2.5],
                },
            },
        }

    def read(self, profile):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(profile))
            return load_profile(path)

    def test_optional_floor_profile_round_trips_without_changing_the_base(self):
        profile = self.profile()
        self.assertEqual(self.read(profile), profile)
        del profile["floor"]
        self.assertEqual(self.read(profile), profile)

    def test_bad_dimensions_and_singular_grip_curves_are_rejected_before_use(self):
        for mutation in (
            lambda p: p["floor"].update(width=0),
            lambda p: p["floor"].update(coefficients=[]),
            lambda p: p["floor"].update(scales=[1] * 7),
            lambda p: p["floor"].update(centers=[[float("nan")] * 8]),
            lambda p: p["floor"]["grip"].update(neutral=[0.2, 0.3, 0.3]),
            lambda p: p["floor"]["grip"].update(pitch=[0, 0.78, 0.78]),
        ):
            profile = copy.deepcopy(self.profile())
            mutation(profile)
            with self.assertRaises(ValueError):
                self.read(profile)

    def test_optional_yaw_correction_is_bounded_and_validated(self):
        profile = self.profile()
        profile["yaw"] = {"version": 1, "width": 0.15, "centers": [[0, 0, 1, 0, 0]], "coefficients": [0.3]}
        self.assertEqual(self.read(profile), profile)
        for change in (
            {"width": 0},
            {"centers": [[0, 1]]},
            {"coefficients": []},
            {"coefficients": [float("nan")]},
            {"coefficients": [5]},
        ):
            invalid = copy.deepcopy(profile)
            invalid["yaw"].update(change)
            with self.assertRaises(ValueError):
                self.read(invalid)


if __name__ == "__main__":
    unittest.main()
