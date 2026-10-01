# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Operator setup only: capture a manually positioned, rehearsed drop station."""

import json
import math
import os
import time

import numpy as np
from innate_skills.box_marker import (
    CONFIG_PATH,
    DICTIONARY,
    MarkerPose,
    camera_matrix,
    front_tag_target,
    rotation_distance,
    validate_config,
)
from innate_skills.drop_in_box_fast import DropInBoxFast

from innate import Head, MainImage, Mobility, Skill, SkillReturn, vision
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W


class TeachBoxMarker(Skill):
    """Record the tag on the centre of the box front, from its current visible position. Does not drive or move the arm."""

    head: Head
    main_image: MainImage | None
    mobility: Mobility

    def execute(self, marker_id: int = 1, marker_size_m: float = 0.08) -> SkillReturn:
        """Measure the black square edge in metres. Use DICT_4X4_50.
        The robot need not be at the drop position. Mount the tag at the
        centre of the box near/front face. Keep
        the marker rigidly attached, facing the camera and visible there.
        This captures the base pose; it does not physically validate the arm.
        """
        p = DropInBoxFast._p
        detector = MarkerPose(marker_id, marker_size_m, p["tilt_deg"])
        self.mobility.stop()
        self.head.set_position(int(round(p["tilt_deg"])))
        try:
            self.sleep(0.3)
            deadline = time.monotonic() + 5
            raw = self.main_image
            samples = []
            while time.monotonic() < deadline:
                self.sleep(0.03)
                image = self.main_image
                if not image or image is raw:
                    continue
                raw = image
                pose = detector.detect(vision.b64_to_gray(image))
                if pose is None:
                    samples.clear()
                    continue
                if samples and (
                    np.linalg.norm(pose[:3, 3] - samples[-1][:3, 3]) > 0.008
                    or rotation_distance(pose, samples[-1]) > math.radians(3)
                ):
                    samples.clear()
                samples.append(pose)
                if len(samples) < 3:
                    continue
                config = validate_config(
                    dict(
                        version=1,
                        dictionary=DICTIONARY,
                        marker_id=marker_id,
                        marker_size_m=marker_size_m,
                        image_size=[IMG_W, IMG_H],
                        camera_matrix=camera_matrix().tolist(),
                        head_tilt_deg=p["tilt_deg"],
                        near_xy=[p["sweet_x"], p["box_y"]],
                        base_from_marker=pose.tolist(),
                    )
                )
                config = front_tag_target(config)
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = CONFIG_PATH.with_suffix(".json.pending")
                tmp.write_text(json.dumps(config, indent=2) + "\n")
                os.replace(tmp, CONFIG_PATH)
                return f"Taught box marker {marker_id}. Learned front tag and 23 cm docking target; physical release still needs a supervised rehearsal."
            raise SkillFailed("Could not get three stable marker poses; previous teaching preserved")
        finally:
            self.mobility.stop()
            self.head.set_position(0)
