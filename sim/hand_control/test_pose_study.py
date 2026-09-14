import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import numpy as np  # noqa: E402
from engine import ArmWorld  # noqa: E402
from pose_study import StudyStore, catalogue  # noqa: E402
from server import ControlSession  # noqa: E402


class PoseStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = ArmWorld()
        cls.poses = catalogue(cls.world)

    def test_every_study_pose_is_physically_reachable_and_motion_stops_on_lost_input(self):
        self.assertEqual(len(self.poses), 16)
        for pose in self.poses:
            for _ in range(240):
                before = np.array([self.world.sim.joint_targets()[n] for n in self.world.names])
                self.world.show_pose(pose)
                self.world.tick(1 / 60)
                after = np.array([self.world.sim.joint_targets()[n] for n in self.world.names])
                self.assertLessEqual(float(np.max(np.abs(after - before))), 1 / 60 + 1e-9)
            actual = self.world.snapshot()
            for name, angle in pose["joints"].items():
                self.assertAlmostEqual(actual["joints"][name], angle, delta=0.035, msg=pose["id"])
            np.testing.assert_allclose(actual["ee"], pose["ee"], atol=0.005)
        self.world.tick(1 / 60, now=time.monotonic() + 1)
        self.assertFalse(self.world.active)
        self.assertIsNone(self.world.demonstration)

    def test_only_the_owner_can_select_whitelisted_poses_and_live_mapping_is_blocked(self):
        control = ControlSession(self.world, self.poses)
        owner, stranger = object(), object()
        token = control.handle(owner, {"op": "begin", "mode": "study"})["token"]
        command = {"op": "study_pose", "token": token, "pose_id": "neutral", "sequence": 1, "captured_at": 1000}
        with self.assertRaises(ValueError):
            control.handle(stranger, command, wall_ms=1000)
        with self.assertRaises(ValueError):
            control.handle(owner, {**command, "pose_id": "arbitrary"}, wall_ms=1000)
        control.handle(owner, command, wall_ms=1000)
        self.assertEqual(control.study_pose_id, "neutral")
        # A slow encoding/review task may keep ownership, never motion.
        control.expire(time.monotonic() + 4)
        self.assertIs(control.owner, owner)
        self.world.tick(1 / 60, now=time.monotonic() + 1)
        self.assertFalse(self.world.active)
        with self.assertRaises(ValueError):
            control.handle(owner, {**command, "op": "move", "sequence": 2}, wall_ms=1000)
        control.handle(owner, {"op": "stop"})
        self.assertFalse(self.world.active)

    def test_floor_exercise_preserves_general_catalogue_and_reaches_all_seven_poses(self):
        floor = catalogue(self.world, "floor")
        self.assertEqual(len(floor), 7)
        self.assertEqual(floor[-1]["role"], "repeat_check")
        self.assertFalse({p["id"] for p in floor} & {p["id"] for p in self.poses})
        for pose in floor:
            for _ in range(300):
                self.world.show_pose(pose)
                self.world.tick(1 / 60)
            actual = self.world.snapshot()
            np.testing.assert_allclose(actual["ee"], pose["ee"], atol=0.005)
            for name, angle in pose["joints"].items():
                self.assertAlmostEqual(actual["joints"][name], angle, delta=0.035, msg=pose["id"])
        with tempfile.TemporaryDirectory() as tmp:
            general = StudyStore(tmp, self.poses)
            original = general.open()
            extra = StudyStore(Path(tmp) / "floor", floor).open()
            self.assertEqual(general.open(original["session"]), original)
            self.assertNotEqual(original["catalogue_hash"], extra["catalogue_hash"])

    def test_local_takes_round_trip_and_redo_preserves_previous_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StudyStore(tmp, self.poses)
            manifest = store.open()
            points = [{"x": 0.5, "y": 0.5, "z": 0.0}] * 21
            frame = {
                "captured_at": 1000,
                "video_time": 0.1,
                "inference_ms": 20,
                "landmarks": points,
                "world_landmarks": points,
            }
            message = {
                "session": manifest["session"],
                "pose_id": "neutral",
                "comfort": "comfortable",
                "frames": [frame] * 8,
                "video": base64.b64encode(b"\x1a\x45\xdf\xa3" + b"x" * 200).decode(),
                "mime": "video/webm;codecs=vp8",
            }
            first = store.save(message)
            second = store.save({**message, "comfort": "awkward"})
            self.assertNotEqual(first["path"], second["path"])
            self.assertTrue(Path(first["path"]).is_file())
            data = json.loads(Path(second["path"]).read_text())
            self.assertEqual(data["target"], self.poses[0])
            self.assertEqual(len(data["frames"]), 8)
            self.assertTrue((Path(second["path"]).parent / data["video"]).is_file())
            self.assertEqual(store.open(manifest["session"])["matches"]["neutral"]["comfort"], "awkward")
            skipped = store.save({"session": manifest["session"], "pose_id": "pitch_up", "comfort": "unmatchable"})
            self.assertEqual(skipped["matches"]["pitch_up"]["frames"], 0)
            with self.assertRaises(ValueError):
                store.open("../../outside")
            with self.assertRaises(ValueError):
                store.save({**message, "frames": []})
            with self.assertRaises(ValueError):
                store.save({**message, "video": "invalid base64"})


if __name__ == "__main__":
    unittest.main()
