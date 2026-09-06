"""The paired renderer must not publish across an in-place world reset."""

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mujoco")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.world_server import WorldServer


@pytest.mark.parametrize("kind,camera", [("rgbd", "main"), ("jpeg", "wrist")])
def test_reset_invalidates_cached_and_inflight_pair(kind, camera):
    product = f"{kind}:{camera}"
    server = object.__new__(WorldServer)
    server.lock = threading.Lock()
    server.frame_ready = threading.Condition()
    server.latest = {product: ({"old": True}, b"old")}
    server.wanted = set()
    server.requested_at = {}
    server.render_demand = threading.Event()

    class Scene:
        world_epoch = 0
        interrupt = True

        def update_camera(self, camera):
            assert server.lock.locked()

        def update_depth(self, camera, *, include_robot):
            assert server.lock.locked() and include_robot

        def read_rgb(self):
            assert not server.lock.locked()
            if self.interrupt:
                server.handle({"op": "reset"})
                assert product not in server.latest
            return np.zeros((480, 640, 3), np.uint8)

        def read_depth(self):
            assert not server.lock.locked()
            return np.ones((120, 160), np.float32)

        def reset(self):
            self.world_epoch += 1

    server.sim = Scene()
    server._render_product(product)
    assert product not in server.latest
    assert product in server.wanted
    server.sim.interrupt = False
    server._render_product(product)
    meta, blob = server.latest[product]
    assert meta["world_epoch"] == 1 and meta["captured_ns"] > 0
    if kind == "rgbd":
        assert len(blob) == meta["jpeg_size"] + 120 * 160 * 4
    # Automatic physics resets bypass the RPC invalidation path.
    server.sim.reset()
    result = []
    reader = threading.Thread(target=lambda: result.append(server.render(camera, kind)))
    reader.start()
    deadline = time.monotonic() + 1
    while product in server.latest and time.monotonic() < deadline:
        time.sleep(0.001)
    assert product not in server.latest
    server._render_product(product)
    reader.join(1)
    assert not reader.is_alive()
    assert result[0][0]["world_epoch"] == 2


def test_surface_depth_preserves_robot_occluder_while_navigation_omits_it():
    import mujoco
    from mars_sim_driver.core import VirtualMars

    scene = object.__new__(VirtualMars)
    scene.model = mujoco.MjModel.from_xml_string("""<mujoco><worldbody>
      <camera name="main" pos="0 0 0"/>
      <geom type="box" pos="0 0 -1" size=".2 .2 .05" group="0"/>
      <geom type="box" pos="0 0 -2" size="2 2 .05" group="1"/>
    </worldbody></mujoco>""")
    scene.data = mujoco.MjData(scene.model)
    mujoco.mj_forward(scene.model, scene.data)
    scene._depth_renderer = None
    scene._depth_w = scene._depth_h = 64
    try:
        scene.update_depth("main", include_robot=True)
        surface = scene.read_depth().copy()
        scene.update_depth("main")
        nav = scene.read_depth().copy()
        assert surface[32, 32] == pytest.approx(0.95, abs=0.001)
        assert nav[32, 32] == pytest.approx(1.95, abs=0.001)
    finally:
        if scene._depth_renderer is not None:
            scene._depth_renderer.close()
