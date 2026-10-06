# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Subscriptions that can be torn down while the owning node keeps spinning."""

import threading
from collections.abc import Callable, Iterable
from typing import Any

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import QoSProfile

Subscription = tuple[type, str, Callable[[Any], None], QoSProfile | int]


class SubscriptionFeed:
    """Subscriptions on a private node and executor, alive until close().

    close() stops the private spin before destroying them: a destroy under the
    owner's multi-threaded executor races its take (InvalidHandle).
    """

    def __init__(self, name: str, subscriptions: Iterable[Subscription]) -> None:
        # Without global arguments: launch's __node remap would otherwise give this node the owner's name.
        self._node = rclpy.create_node(name, use_global_arguments=False, start_parameter_services=False)
        for msg_type, topic, callback, qos in subscriptions:
            self._node.create_subscription(msg_type, topic, callback, qos)
        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._executor.shutdown()
        self._thread.join()
        self._node.destroy_node()
