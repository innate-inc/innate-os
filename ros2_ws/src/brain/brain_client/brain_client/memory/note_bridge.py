# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS interface shared by Map and Teleop; all writes use the same store."""

import base64
import json
import sqlite3

from brain_messages.srv import MapNotes as MapNotesService
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class NoteBridge:
    def __init__(self, node, notes):
        self.notes = notes
        self.pub = node.create_publisher(String, "/brain/map_notes", LATCHED_QOS)
        self.service = node.create_service(MapNotesService, "/brain/map_notes", self.call)
        self.timer = node.create_timer(0.25, self.publish)
        self.signature = None

    def publish(self):
        try:
            snapshot = self.notes.snapshot()
        except (sqlite3.Error, OSError):
            return
        signature = (str(snapshot["map_ref"]), snapshot["revision"])
        if signature != self.signature:
            self.pub.publish(String(data=json.dumps(snapshot)))
            self.signature = signature

    def call(self, request, response):
        try:
            if len(request.request) > 16000:
                raise ValueError("request too long")
            payload = json.loads(request.request)
            operation = payload["operation"]
            if operation == "snapshot":
                result = {"ok": True, **self.notes.snapshot()}
            else:
                result = self.notes.execute(
                    operation,
                    payload.get("arguments", {}),
                    ref=payload.get("map_ref"),
                    call_id=payload.get("request_id"),
                    operator=True,
                )
                if result.get("ok") and operation in ("write_map_note", "remove_map_note"):
                    result["snapshot"] = self.notes.snapshot()
                if "images" in result:
                    result["images"] = [
                        {"id": key, "jpeg": base64.b64encode(image).decode()} for key, image in result["images"]
                    ]
            response.response = json.dumps(result)
            self.publish()
        except (sqlite3.Error, OSError):
            response.response = json.dumps({"ok": False, "error": "STORE_UNAVAILABLE"})
        except (ValueError, KeyError, TypeError):
            response.response = json.dumps({"ok": False, "error": "INVALID_ARGUMENT"})
        return response
