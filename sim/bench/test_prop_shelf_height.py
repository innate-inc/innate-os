"""Scene editing can select a shelf without changing challenge drop defaults."""

import math
from unittest.mock import Mock

import pytest
from mars_sim_driver.props import Prop, PropRegistry


def registry():
    props = PropRegistry({"jar": Prop("jar", drop_z=0.194)})
    props._addr["jar"] = (0, 0, 0, None)
    props._set_pose = Mock()
    return props


def test_explicit_shelf_height_does_not_change_later_default_drops():
    props = registry()
    data = object()
    assert props.drop_at(data, "jar", -2.24, 0.3, z=0.316)
    props._set_pose.assert_called_with(data, "jar", -2.24, 0.3, 0.316, 0.0)
    assert props.drop_at(data, "jar", -2.24, 0.3)
    props._set_pose.assert_called_with(data, "jar", -2.24, 0.3, 0.194, 0.0)


@pytest.mark.parametrize("height", [math.nan, math.inf, -math.inf])
def test_nonfinite_shelf_height_does_not_touch_physics(height):
    props = registry()
    with pytest.raises(ValueError, match="finite"):
        props.drop_at(object(), "jar", 0, 0, z=height)
    props._set_pose.assert_not_called()


def test_retry_restores_jars_left_and_only_boxes_on_other_shelves(tmp_path):
    import threading
    from pathlib import Path

    from mars_sim_driver.challenges import ChallengeEngine
    from mars_sim_driver.core import VirtualMars
    from mars_sim_driver.environments import Environment

    sim_root = Path(__file__).resolve().parents[1]
    mars = VirtualMars(render_wh=(64, 48), environment=Environment.load("pantry", sim_root / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "progress.json"
    )
    try:
        for _ in range(2):
            assert engine.start("pantry_count_jars")
            mars.step(1.0)
            objects = mars.object_poses()
            jars = {name: pos for name, pos in objects.items() if "_jar_" in name}
            boxes = {name: pos for name, pos in objects.items() if "_carton_" in name}
            assert len(objects) == 9 and len(jars) == 5 and len(boxes) == 4
            assert all(abs(pos[0] + 2.24) < 0.002 for pos in jars.values())
            for name, pos in jars.items():
                assert abs(pos[2] - mars.props.props[name].rest_z - 0.26) < 0.001
            assert sum(abs(pos[1] - 1.54) < 0.002 for pos in boxes.values()) == 2
            assert sum(abs(pos[0] - 2.24) < 0.002 for pos in boxes.values()) == 2
            # Reproduce the old bad arrangement before retrying.
            mars.drop_prop_at("pantry_jar_stray", 0.25, 1.54, z=0.165)
    finally:
        mars.close()
