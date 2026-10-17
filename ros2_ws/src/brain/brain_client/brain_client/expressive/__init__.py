# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Expressive motion for MARS: text -> recipe -> plan -> lively 25 Hz motion -> actuator poses.

Pure Python (numpy only; mujoco optional for ``reach``), no ROS: the brain client, the host tools
(``expressive/``) and the 5090 server import the same code. See docs/EXPRESSIVE.md.
"""
