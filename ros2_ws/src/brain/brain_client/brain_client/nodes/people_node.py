#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people node: who is in front of the robot, published for the brain to read.

Its own process because every face and body model is a native library — a
segfault here must not take the brain, TTS and chat with it — and because it
wants frames at its own rate rather than the turn loop's.

One timer does everything: take the newest left-eye frame, ask the recognizer
who is in it, publish a latched snapshot. Recognition costs ~100 ms and the
services answer from memory, so a call waits at most one tick and the node
stays single-threaded.
"""

from __future__ import annotations

import json
import threading
import time

import cv2
import numpy as np
import rclpy
from brain_messages.srv import ForgetPerson, RenamePerson
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from brain_client.people.models import load_models
from brain_client.people.recognize import Recognizer, Sighting
from brain_client.people.roster import Roster, default_dir

IMAGE_TOPIC = "/mars/main_camera/left/image_raw/compressed"
SNAPSHOT_TOPIC = "/brain/people"
SNAPSHOT_SCHEMA = 1

_PARAMS: dict[str, bool | float] = {
    "simulator_mode": False,
    "allow_model_download": True,
    "tick_hz": 2.0,
}
_SENSOR_QOS = QoSProfile(depth=1, history=QoSHistoryPolicy.KEEP_LAST, reliability=QoSReliabilityPolicy.BEST_EFFORT)
_LATCHED_QOS = QoSProfile(
    depth=1,
    history=QoSHistoryPolicy.KEEP_LAST,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


class PeopleNode(Node):
    def __init__(self) -> None:
        super().__init__("people_node")
        for name, default in _PARAMS.items():
            self.declare_parameter(name, default)
        simulator = bool(self.get_parameter("simulator_mode").value)
        tick_hz = max(0.2, float(self.get_parameter("tick_hz").value))  # type: ignore[arg-type]

        directory = default_dir(simulator=simulator)
        self._roster = Roster(directory)
        models = load_models(allow_download=bool(self.get_parameter("allow_model_download").value))
        self._recognizer = Recognizer(models, self._roster)
        self._health = models.health

        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._sightings: list[Sighting] = []

        self._snapshot_pub = self.create_publisher(String, SNAPSHOT_TOPIC, _LATCHED_QOS)
        self.create_subscription(CompressedImage, IMAGE_TOPIC, self._on_image, _SENSOR_QOS)
        self.create_service(RenamePerson, "/brain/people/rename", self._on_rename)
        self.create_service(ForgetPerson, "/brain/people/forget", self._on_forget)
        self.create_timer(1.0 / tick_hz, self._tick)

        self._publish()
        self.get_logger().info(
            f"\033[1;92m[People] up: {self._roster.named} named + {len(self._roster) - self._roster.named} unnamed "
            f"on file under {directory}, faces {self._health['faces']}, outfits {self._health['outfits']}\033[0m"
        )

    def _on_image(self, msg: CompressedImage) -> None:
        if msg.data:
            with self._lock:
                self._frame = bytes(msg.data)

    def _tick(self) -> None:
        with self._lock:
            data, self._frame = self._frame, None
        if data is None:
            self._publish()  # a heartbeat: a snapshot that stops means the node did, not that the room is empty
            return
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return
        self._sightings = self._recognizer.look(frame, time.time())
        self._publish()

    def _publish(self) -> None:
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "stamp": time.time(),
            "health": self._health,
            "people": [
                {
                    "person_id": sighting.person_id,
                    "name": sighting.name,
                    "state": sighting.state,
                    "where": _where(sighting),
                }
                for sighting in self._sightings
            ],
        }
        self._snapshot_pub.publish(String(data=json.dumps(payload)))

    def _on_rename(self, request: RenamePerson.Request, response: RenamePerson.Response) -> RenamePerson.Response:
        person_id, message = self._target(request.who)
        if person_id is None:
            response.success, response.message = False, message
            return response
        name = request.name.strip()
        if not name:
            response.success, response.message = False, "I need a name to give them."
            return response
        response.success = self._roster.rename(person_id, name)
        response.person_id = person_id
        response.message = f"Noted, that is {name}." if response.success else "I lost that person on the way."
        return response

    def _on_forget(self, request: ForgetPerson.Request, response: ForgetPerson.Response) -> ForgetPerson.Response:
        person_id, message = self._target(request.who)
        if person_id is None:
            response.success, response.message = False, message
            return response
        response.success = self._roster.forget(person_id)
        response.message = "Done, I have forgotten them." if response.success else "I lost that person on the way."
        return response

    def _target(self, who: str) -> tuple[str | None, str]:
        """The person a mutation is about: the one named, or — when ``who`` is
        empty — whoever is in view, which is what "this is Theo" means. Only a
        face will do there, never an outfit match, and never two people at
        once: naming the wrong person is worse than asking again."""
        if who.strip():
            person_id = self._roster.resolve(who.strip())
            return (person_id, "" if person_id else f"I have nobody on file called {who}.")
        known = {sighting.person_id for sighting in self._sightings if sighting.state == "known"}
        if len(known) == 1:
            return (known.pop(), "")
        return (None, "I need to be looking at one person." if known else "I cannot see anyone I recognize.")


def _where(sighting: Sighting) -> str:
    """Enough for the brain to tell two people apart when it speaks."""
    centre = (sighting.box[1] + sighting.box[3]) / 2.0
    if centre < 0.4:
        return "left"
    return "right" if centre > 0.6 else "ahead"


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = PeopleNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down.")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
