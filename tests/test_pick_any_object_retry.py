import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))

from innate_skills.pick_any_object import PARAMS, PickAnyObject


class _Manipulation:
    GRIPPER_OPEN = 0.8

    def __init__(self, skill, *, closes_empty, empties_on_lift=False):
        self.skill = skill
        self.pose = SimpleNamespace(z=0.04)
        self.closes_empty = closes_empty
        self.empties_on_lift = empties_on_lift
        self.events = []

    def gripper_open(self, duration):
        self.events.append(("open", duration))

    def gripper_close(self, strength, duration):
        self.events.append(("close", strength, duration))
        self.skill.joint_states.position[5] = -0.085 if self.closes_empty else 0.1

    def follow(self, waypoints, grip):
        self.events.append(("follow", [wp.z for wp in waypoints], grip))
        self.pose.z = waypoints[-1].z

    def move_to(self, _x, _y, z, **_kwargs):
        self.events.append(("move_to", z))
        self.pose.z = z

    def move_joints(self, _joints, duration):
        self.events.append(("lift", duration))
        self.pose.z = 0.22
        if self.empties_on_lift:
            self.skill.joint_states.position[5] = -0.085


def _skill(*, closes_empty, empties_on_lift=False):
    skill = PickAnyObject.__new__(PickAnyObject)
    skill._p = dict(PARAMS)
    skill.joint_states = SimpleNamespace(position=[0.0] * 6)
    skill.logger = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    skill.check_cancelled = lambda: None
    skill._grip_strength = 0.0
    skill._holding = False
    skill.manipulation = _Manipulation(skill, closes_empty=closes_empty, empties_on_lift=empties_on_lift)
    return skill


def test_air_close_retries_once_lower_but_held_object_does_not(monkeypatch):
    monkeypatch.setattr("innate_skills.pick_any_object.time.sleep", lambda _seconds: None)

    missed = _skill(closes_empty=True)
    missed._close_once()
    assert missed._retry_lower_if_empty(0.25, 0.0, 0.0, 1.3, 0.0)
    assert missed.manipulation.events == [
        ("close", 0.6, 1.5),
        ("open", 1.0),
        ("follow", [0.02], 0.8),
        ("move_to", 0.03),
        ("close", 0.6, 1.5),
    ]

    held = _skill(closes_empty=False)
    held._close_once()
    assert not held._retry_lower_if_empty(0.25, 0.0, 0.0, 1.3, 0.0)
    assert held.manipulation.events == [("close", 0.6, 1.5)]

    dropped = _skill(closes_empty=False, empties_on_lift=True)
    dropped._close_twist_lift(0.25, 0.0, 0.0, 1.3, 0.0)
    assert [event[0] for event in dropped.manipulation.events].count("open") == 1
    assert [event[0] for event in dropped.manipulation.events].count("close") == 2
    assert [event[0] for event in dropped.manipulation.events].count("lift") == 2
