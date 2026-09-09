#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Composition root for the people node.

The robot's subconscious for people: who is in the room, who they are, what it
knows about them (docs/rfc/people-memory.md in innate-jetson). Its own process
because every face and body model is a native library — a segfault here must
not take the brain, TTS and chat with it — and because it wants frames and a
lifecycle the brain's turn loop does not.

This file does no behaviour of its own: it reads the parameters, builds the
store, the backends, the engine and the scribe, hands them to the adapters in
``brain_client/people/node_adapters.py``, and spins. The brain runs
unannotated when this node is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import rclpy
from rclpy.node import Node

from brain_client.brain.transport import pick_rest
from brain_client.people.backends import load_backends
from brain_client.people.engine import EngineConfig, PeopleEngine
from brain_client.people.geometry import CameraModel
from brain_client.people.node_adapters import PARAM_DEFAULTS, PeopleAdapters, config_from_params
from brain_client.people.scribe import Scribe
from brain_client.people.store import PeopleStore
from brain_client.people.track import Tracker

if TYPE_CHECKING:
    from brain_client.people.scribe import Transport

_ACCESSOR = {str: "string_value", bool: "bool_value", int: "integer_value", float: "double_value"}
_QUEUE_FILE = "scribe_queue.jsonl"
_SKIPPABLE_SPIN_ERRORS = ("RCLError", "InvalidHandle")


class PeopleNode(Node):
    def __init__(self) -> None:
        super().__init__("people_node")
        for name, default in PARAM_DEFAULTS.items():
            self.declare_parameter(name, default)
        self.config = config_from_params(
            {
                name: getattr(self.get_parameter(name).get_parameter_value(), _ACCESSOR[type(default)])
                for name, default in PARAM_DEFAULTS.items()
            }
        )
        store = PeopleStore(
            self.config.data_dir,
            retention_unnamed_days=self.config.retention_unnamed_days,
            retention_named_days=self.config.retention_named_days,
        )
        # Blocking, and on a robot provisioned without the model files this is
        # one 30 s download before the node spins — which is why provisioning
        # ships them and allow_model_download only covers the gap.
        self.get_logger().info(f"[People] loading models from {self.config.models_dir}")
        backends = load_backends(
            self.config.prefer_backend, self.config.models_dir, allow_download=self.config.allow_model_download
        )
        engine_config = EngineConfig()
        engine = PeopleEngine(
            backends,
            store,
            camera=CameraModel.published_default(self.config.camera_height_m),
            # The counter resumes where the last run left it: this node respawns
            # under launch and its latched snapshot outlives the restart, so a
            # reissued P<n> would name a stranger to a skill still holding it.
            tracker=Tracker(first_tag=store.next_tag()),
            config=engine_config,
        )
        transport = self._transport()
        scribe = None
        if self.config.scribe and transport is not None:
            scribe = Scribe(
                store,
                transport,
                model=self.config.gemini_model,
                queue_path=self.config.data_dir / _QUEUE_FILE,
            )
        elif self.config.scribe:
            self.get_logger().warning("[People] no Gemini transport: the scribe is off, recognition is not")

        self.adapters: PeopleAdapters = PeopleAdapters(
            self,
            self.config,
            store=store,
            engine=engine,
            engine_config=engine_config,
            scribe=scribe,
            transport=transport,
        )
        self.adapters.start()
        named, unnamed = store.counts()
        self.get_logger().info(
            f"\033[1;92m[People] people_node up: {named} named + {unnamed} unnamed on file, "
            f"faces {backends.health.get('face_model')}, bodies {backends.health.get('body_model')}, "
            f"ticking on {self.config.image_topic}\033[0m"
        )

    def _transport(self) -> Transport | None:
        """The same Gemini path the brain uses: the proxy when it is
        configured, ``GEMINI_API_KEY`` otherwise, and None when neither is."""
        from innate_proxy import ProxyClient

        proxy = None
        try:
            candidate = ProxyClient()
            proxy = candidate if candidate.is_available() else None
        except Exception as error:  # noqa: BLE001 — a proxy that will not build costs the scribe, not the engine
            self.get_logger().warning(f"[People] could not initialize the proxy: {error}")
        rest = pick_rest(proxy)
        return rest.post if rest is not None else None

    def destroy_node(self) -> bool:
        self.adapters.shutdown()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = PeopleNode()
    try:
        # Manual spin so a transient middleware error is logged and skipped
        # instead of killing the node, exactly as brain_client_node does: a
        # corrupted CompressedImage on either camera topic (RCLError), and an
        # entity torn down between two spins (InvalidHandle).
        while rclpy.ok():
            try:
                rclpy.spin_once(node, timeout_sec=0.5)
            except KeyboardInterrupt:
                raise
            except Exception as error:
                if not any(name in type(error).__name__ for name in _SKIPPABLE_SPIN_ERRORS):
                    raise
                node.get_logger().warn(f"Skipping {type(error).__name__} (message dropped): {error}")
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down.")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
