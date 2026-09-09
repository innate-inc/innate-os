# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The camera ring read on one thread while another fills it.

``CameraCapture`` is written by the image callback on the ROS executor thread
and read by the brain's agent thread when it pairs a people snapshot with the
frame it was measured on. The readers take a snapshot of the deque instead of a
lock, and this is the test that says why they have to: iterating the deque
itself dies the moment a frame arrives underneath it.
"""

import importlib.abc
import importlib.util
import sys
import threading
import time
from collections.abc import MutableSequence
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROS_ROOTS = frozenset({"geometry_msgs", "rclpy", "sensor_msgs", "std_msgs"})


class _StubModule(ModuleType):
    """A module whose every attribute is a mock, and which is a package so the
    submodule imports below it keep resolving."""

    __path__: MutableSequence[str] = []

    def __getattr__(self, name: str) -> MagicMock:
        value = MagicMock()
        setattr(self, name, value)
        return value


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec) -> ModuleType:
        return _StubModule(spec.name)

    def exec_module(self, module: ModuleType) -> None:
        pass


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.partition(".")[0] not in _ROS_ROOTS:
            return None
        return importlib.util.spec_from_loader(fullname, _StubLoader())


# Appended, never inserted: where ROS is installed its own finder answers first.
_STUB_FINDER = _StubFinder()
sys.meta_path.append(_STUB_FINDER)

from brain_client.perception import camera as cam  # noqa: E402 — needs the stubs above

# The stubs live exactly as long as the import above: every other test module
# in this session must go on finding ROS missing, because it is.
sys.meta_path.remove(_STUB_FINDER)
for _stubbed in [name for name, module in sys.modules.items() if isinstance(module, _StubModule)]:
    del sys.modules[_stubbed]

_UNKNOWN_STAMP = 1  # no frame ever carries it, so every read walks the whole ring
_RACE_SEC = 0.5  # the unfixed reader raises inside a tenth of that


@pytest.fixture
def eager_thread_switches():
    """Hand the GIL over roughly every bytecode, so a mutation lands inside a
    reader's loop within a second instead of once in a blue moon."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


def capture() -> cam.CameraCapture:
    config = SimpleNamespace(
        image_topic="/main/compressed",
        arm_camera_image_topic="/arm/compressed",
        send_arm_camera_image=False,
        cmd_vel_topic="/cmd_vel",
    )
    return cam.CameraCapture(MagicMock(), config)


def image(stamp_ns: int) -> SimpleNamespace:
    stamp = SimpleNamespace(sec=stamp_ns // 10**9, nanosec=stamp_ns % 10**9)
    return SimpleNamespace(data=b"jpeg", header=SimpleNamespace(stamp=stamp))


def test_the_ring_can_be_read_while_the_executor_thread_fills_it(eager_thread_switches):
    """The brain pairs frames on its own thread; the camera callback appends on
    the executor's. Neither may make the other raise."""
    ring = capture()
    stop = threading.Event()
    stamp = 10**9
    ring._on_image(image(stamp))

    def fill() -> None:
        nonlocal stamp
        while not stop.is_set():
            stamp += 10**8
            ring._on_image(image(stamp))

    writer = threading.Thread(target=fill, name="fake-executor", daemon=True)
    writer.start()
    try:
        reads = 0
        deadline = time.monotonic() + _RACE_SEC
        while time.monotonic() < deadline:
            assert ring.frame_for_stamp(_UNKNOWN_STAMP, 3.0) is None
            ring.fresh_frame(3.0)
            reads += 1
    finally:
        stop.set()
        writer.join(timeout=5.0)
    assert reads > 100  # the reads really happened: the writer did not starve them


def test_a_reader_survives_the_ring_emptying_underneath_it(eager_thread_switches):
    """``stop()`` clears the ring from the executor thread, and a turn already
    under way goes on reading it."""
    ring = capture()
    stop = threading.Event()

    def churn() -> None:
        stamp = 10**9
        while not stop.is_set():
            stamp += 10**8
            ring._on_image(image(stamp))
            ring.stop()

    executor = threading.Thread(target=churn, name="fake-executor", daemon=True)
    executor.start()
    try:
        deadline = time.monotonic() + _RACE_SEC
        while time.monotonic() < deadline:
            ring.frame_for_stamp(_UNKNOWN_STAMP, 3.0)
            ring.fresh_frame(3.0)
    finally:
        stop.set()
        executor.join(timeout=5.0)
