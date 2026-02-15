#!/usr/bin/env python3
"""
Open-vocabulary grasp skill using RGB + depth.

Pipeline:
1) Gemini localizes the user-requested object in RGB.
2) Depth-based scoring selects a top-down grasp point in that bbox.
3) Optionally dispatches an existing grasp behavior.
"""

import json
import math
import os
import re
from urllib import error, request

import cv2
import numpy as np
import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient

from brain_messages.action import ExecuteBehavior
from brain_client.skill_types import RobotState, RobotStateType, Skill, SkillResult


class GraspObjectOpenVocab(Skill):
    """
    Select an object by language and generate a depth-based grasp candidate.

    This skill can optionally trigger an existing behavior policy if the
    target maps cleanly to one.
    """

    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
    depth = RobotState(RobotStateType.LAST_DEPTH_IMAGE)

    CONTRACT_VERSION = "1.0"
    _BEHAVIOR_KEYWORDS = {
        "sock": "pick_socks",
        "socks": "pick_socks",
        "paper": "pick_paper",
        "trash": "pick_paper",
        "tissue": "pick_paper",
        "napkin": "pick_paper",
    }

    def __init__(self, logger):
        super().__init__(logger)
        self._cancel_requested = False
        self._goal_handle = None
        self._action_client = None
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

        if not self.gemini_api_key:
            self.logger.warning(
                "GEMINI_API_KEY is not set. grasp_object_open_vocab will fail until configured."
            )

    @property
    def name(self):
        return "grasp_object_open_vocab"

    def guidelines(self):
        return (
            "Use this to grasp a natural-language target (for example 'black sock' or "
            "'piece of paper near the table'). The skill uses Gemini for RGB localization "
            "and depth for grasp point scoring. "
            "Set execute_behavior=true to dispatch a mapped behavior, or false to only plan."
        )

    @staticmethod
    def _extract_json_block(text: str) -> str:
        content = text.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z0-9_+-]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        return content.strip()

    @staticmethod
    def _clamp_bbox_norm(bbox: dict) -> tuple[float, float, float, float] | None:
        required_keys = ["x_min", "y_min", "x_max", "y_max"]
        if not isinstance(bbox, dict) or any(key not in bbox for key in required_keys):
            return None

        try:
            x_min = max(0.0, min(1.0, float(bbox["x_min"])))
            y_min = max(0.0, min(1.0, float(bbox["y_min"])))
            x_max = max(0.0, min(1.0, float(bbox["x_max"])))
            y_max = max(0.0, min(1.0, float(bbox["y_max"])))
        except Exception:
            return None

        if x_max <= x_min or y_max <= y_min:
            return None
        return x_min, y_min, x_max, y_max

    @staticmethod
    def _normalize(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
        result = np.zeros_like(values, dtype=np.float32)
        valid_values = values[mask]
        if valid_values.size == 0:
            return result
        value_min = float(np.min(valid_values))
        value_max = float(np.max(valid_values))
        if value_max <= value_min:
            result[mask] = 1.0
            return result
        result[mask] = (valid_values - value_min) / (value_max - value_min)
        return result

    def _build_detection_prompt(self, target_query: str) -> str:
        return f"""
You are a robotics perception module selecting one grasp target.

Target query: "{target_query}"

Return ONLY valid JSON (no markdown, no explanation) with schema:
{{
  "contract_version": "{self.CONTRACT_VERSION}",
  "selected_object": {{
    "label": "string",
    "confidence": 0.0,
    "bbox_norm": {{
      "x_min": 0.0,
      "y_min": 0.0,
      "x_max": 1.0,
      "y_max": 1.0
    }}
  }},
  "alternates": [
    {{
      "label": "string",
      "confidence": 0.0,
      "bbox_norm": {{
        "x_min": 0.0,
        "y_min": 0.0,
        "x_max": 1.0,
        "y_max": 1.0
      }}
    }}
  ],
  "reason": "short string"
}}

Rules:
- Pick the visible object that best matches the query.
- bbox_norm coordinates are normalized to [0,1].
- If target is absent, set selected_object to null and explain in reason.
- Prefer graspable candidates over tiny or heavily occluded instances.
""".strip()

    def _detect_target_bbox(self, target_query: str, gemini_model: str) -> dict:
        if not self.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        if not self.image:
            raise RuntimeError("No RGB image available for Gemini detection.")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{gemini_model}:generateContent?key={self.gemini_api_key}"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": self._build_detection_prompt(target_query)},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": self.image,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "topP": 1,
                "topK": 1,
            },
        }

        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=90) as response:
                response_json = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini HTTP {exc.code}: {body}") from exc
        except Exception as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        candidates = response_json.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")

        parts = candidates[0].get("content", {}).get("parts", [])
        model_text = ""
        for part in parts:
            if "text" in part:
                model_text += part["text"]
        model_text = model_text.strip()
        if not model_text:
            raise RuntimeError("Gemini response did not contain text.")

        parsed = json.loads(self._extract_json_block(model_text))
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini output is not a JSON object.")
        return parsed

    def _depth_to_meters(self):
        if not isinstance(self.depth, dict):
            raise RuntimeError("Depth state is missing or malformed.")

        depth_array = self.depth.get("array")
        if not isinstance(depth_array, np.ndarray) or depth_array.ndim != 2:
            raise RuntimeError("Depth state does not contain a valid 2D array.")

        encoding = str(self.depth.get("encoding", "")).lower()

        if depth_array.dtype == np.uint16 or encoding in ("16uc1", "mono16"):
            depth_m = depth_array.astype(np.float32) / 1000.0
            units = "mm_to_m"
        elif depth_array.dtype in (np.float32, np.float64) or encoding == "32fc1":
            depth_m = depth_array.astype(np.float32)
            units = "meters_assumed"
        else:
            raise RuntimeError(
                f"Unsupported depth format: dtype={depth_array.dtype}, encoding={encoding}"
            )

        return depth_m, units

    def _infer_behavior(self, target_query: str, selected_label: str) -> str | None:
        merged = f"{target_query} {selected_label}".lower()
        for keyword, behavior_name in self._BEHAVIOR_KEYWORDS.items():
            if keyword in merged:
                return behavior_name
        return None

    def _execute_behavior(self, behavior_name: str, timeout_sec: float):
        if not self.node:
            return SkillResult.FAILURE, "Skill missing ROS node context."

        if not self._action_client:
            self._action_client = ActionClient(self.node, ExecuteBehavior, "/behavior/execute")

        if not self._action_client.wait_for_server(timeout_sec=5.0):
            return SkillResult.FAILURE, "ExecuteBehavior action server not available."

        goal_msg = ExecuteBehavior.Goal()
        goal_msg.behavior_name = behavior_name

        goal_future = self._action_client.send_goal_async(goal_msg)
        rclpy.spin_until_future_complete(self.node, goal_future, timeout_sec=10.0)
        if not goal_future.done():
            return SkillResult.FAILURE, "Timed out waiting for behavior goal acceptance."

        self._goal_handle = goal_future.result()
        if not self._goal_handle or not self._goal_handle.accepted:
            self._goal_handle = None
            return SkillResult.FAILURE, f"Behavior '{behavior_name}' goal rejected."

        result_future = self._goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self.node, result_future, timeout_sec=timeout_sec)
        if not result_future.done():
            if self._goal_handle:
                self._goal_handle.cancel_goal_async()
            self._goal_handle = None
            return SkillResult.FAILURE, f"Behavior '{behavior_name}' timed out."

        result_response = result_future.result()
        self._goal_handle = None
        status = result_response.status
        result = result_response.result
        message = str(getattr(result, "message", ""))

        if status == GoalStatus.STATUS_SUCCEEDED and getattr(result, "success", False):
            return SkillResult.SUCCESS, message
        if status == GoalStatus.STATUS_CANCELED:
            return SkillResult.CANCELLED, message
        if status == GoalStatus.STATUS_ABORTED:
            return SkillResult.FAILURE, message
        if status == GoalStatus.STATUS_SUCCEEDED:
            return SkillResult.FAILURE, message
        return SkillResult.FAILURE, f"Behavior ended with status={status}: {message}"

    def execute(
        self,
        target_query: str,
        gemini_model: str = "gemini-2.5-pro",
        min_detection_confidence: float = 0.45,
        min_depth_m: float = 0.08,
        max_depth_m: float = 1.20,
        top_percentile: float = 0.35,
        min_clearance_px: int = 6,
        execute_behavior: bool = True,
        behavior_name: str = "",
        behavior_timeout_sec: float = 60.0,
    ):
        self._cancel_requested = False

        if not target_query or not target_query.strip():
            return "target_query must be a non-empty string.", SkillResult.FAILURE
        if not self.image:
            return "No RGB image available for object selection.", SkillResult.FAILURE
        if self.depth is None:
            return "No depth image available for grasp planning.", SkillResult.FAILURE
        if min_depth_m <= 0.0 or max_depth_m <= min_depth_m:
            return "Depth limits must satisfy 0 < min_depth_m < max_depth_m.", SkillResult.FAILURE
        if top_percentile <= 0.0 or top_percentile > 1.0:
            return "top_percentile must be in (0.0, 1.0].", SkillResult.FAILURE
        if min_clearance_px < 0:
            return "min_clearance_px must be >= 0.", SkillResult.FAILURE
        if behavior_timeout_sec <= 0.0:
            return "behavior_timeout_sec must be > 0.", SkillResult.FAILURE

        self._send_feedback(f"Selecting '{target_query}' in RGB with {gemini_model}...")

        try:
            detection = self._detect_target_bbox(
                target_query=target_query,
                gemini_model=gemini_model,
            )
        except Exception as exc:
            return f"Gemini target selection failed: {exc}", SkillResult.FAILURE

        selected = detection.get("selected_object")
        if selected is None:
            reason = detection.get("reason", "target not found")
            return f"Target not found: {reason}", SkillResult.FAILURE

        try:
            detected_confidence = float(selected.get("confidence", 0.0))
        except Exception:
            detected_confidence = 0.0

        if detected_confidence < float(min_detection_confidence):
            return (
                f"Detection confidence too low ({detected_confidence:.2f} < {min_detection_confidence:.2f}).",
                SkillResult.FAILURE,
            )

        bbox_norm = self._clamp_bbox_norm(selected.get("bbox_norm", {}))
        if bbox_norm is None:
            return "Gemini returned an invalid bbox_norm.", SkillResult.FAILURE

        try:
            depth_m, depth_units = self._depth_to_meters()
        except Exception as exc:
            return str(exc), SkillResult.FAILURE

        height, width = depth_m.shape
        x_min_n, y_min_n, x_max_n, y_max_n = bbox_norm
        x_min = int(np.clip(round(x_min_n * (width - 1)), 0, width - 1))
        y_min = int(np.clip(round(y_min_n * (height - 1)), 0, height - 1))
        x_max = int(np.clip(round(x_max_n * (width - 1)), 0, width - 1))
        y_max = int(np.clip(round(y_max_n * (height - 1)), 0, height - 1))
        if x_max <= x_min or y_max <= y_min:
            return "Projected bbox is empty for the current depth frame.", SkillResult.FAILURE

        target_mask = np.zeros((height, width), dtype=bool)
        target_mask[y_min : y_max + 1, x_min : x_max + 1] = True

        finite_mask = np.isfinite(depth_m)
        valid_mask = finite_mask & (depth_m > min_depth_m) & (depth_m < max_depth_m) & target_mask
        valid_count = int(np.count_nonzero(valid_mask))
        if valid_count == 0:
            return "No valid depth values inside selected target region.", SkillResult.FAILURE

        valid_depths = depth_m[valid_mask]
        depth_threshold = float(np.quantile(valid_depths, top_percentile))
        candidate_mask = valid_mask & (depth_m <= depth_threshold)

        depth_blur = cv2.GaussianBlur(depth_m, (5, 5), 0)
        grad_x = cv2.Sobel(depth_blur, cv2.CV_32F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(depth_blur, cv2.CV_32F, 0, 1, ksize=3)
        grad_mag = np.sqrt(grad_x * grad_x + grad_y * grad_y)

        valid_u8 = valid_mask.astype(np.uint8)
        clearance_map = cv2.distanceTransform(valid_u8, cv2.DIST_L2, 3)
        candidate_mask &= clearance_map >= float(min_clearance_px)
        candidate_count = int(np.count_nonzero(candidate_mask))
        if candidate_count == 0:
            return "No candidate points after clearance filtering.", SkillResult.FAILURE

        depth_score = 1.0 - self._normalize(depth_m, valid_mask)
        smooth_score = 1.0 - self._normalize(grad_mag, valid_mask)
        clearance_score = self._normalize(clearance_map, valid_mask)
        score = 0.50 * depth_score + 0.30 * smooth_score + 0.20 * clearance_score
        score = score.astype(np.float32)
        score[~candidate_mask] = -np.inf

        best_flat_idx = int(np.argmax(score))
        v, u = np.unravel_index(best_flat_idx, score.shape)
        best_score = float(score[v, u])
        if not math.isfinite(best_score):
            return "Failed to select a valid grasp candidate.", SkillResult.FAILURE

        best_depth_m = float(depth_m[v, u])
        grasp_yaw_rad = float(math.atan2(float(grad_y[v, u]), float(grad_x[v, u])) + (math.pi / 2.0))
        grasp_yaw_rad = float(math.atan2(math.sin(grasp_yaw_rad), math.cos(grasp_yaw_rad)))

        selected_label = str(selected.get("label", "unknown"))
        resolved_behavior = behavior_name.strip() or self._infer_behavior(
            target_query=target_query,
            selected_label=selected_label,
        )

        result_payload = {
            "contract_version": self.CONTRACT_VERSION,
            "target_query": target_query,
            "vlm_detection": detection,
            "target_bbox_px": {
                "x_min": x_min,
                "y_min": y_min,
                "x_max": x_max,
                "y_max": y_max,
            },
            "grasp": {
                "type": "topdown_planar_depth",
                "confidence": round(best_score, 4),
                "pixel": {"u": int(u), "v": int(v)},
                "depth_m": round(best_depth_m, 4),
                "yaw_rad": round(grasp_yaw_rad, 4),
                "yaw_deg": round(math.degrees(grasp_yaw_rad), 2),
                "candidate_count": candidate_count,
                "valid_count": valid_count,
            },
            "behavior": {
                "requested": behavior_name.strip() or None,
                "resolved": resolved_behavior,
                "execute_behavior": bool(execute_behavior),
            },
            "depth_units": depth_units,
            "frame_id": self.depth.get("frame_id", ""),
            "stamp": self.depth.get("stamp"),
        }

        if not execute_behavior:
            return json.dumps(result_payload), SkillResult.SUCCESS

        if not resolved_behavior:
            return (
                f"No behavior mapping found for '{target_query}' (detected '{selected_label}').",
                SkillResult.FAILURE,
            )

        self._send_feedback(f"Dispatching behavior '{resolved_behavior}'...")
        status, message = self._execute_behavior(
            behavior_name=resolved_behavior,
            timeout_sec=float(behavior_timeout_sec),
        )
        result_payload["behavior"]["execution_message"] = message
        result_payload["behavior"]["execution_status"] = status.value

        return json.dumps(result_payload), status

    def cancel(self):
        self._cancel_requested = True
        if self._goal_handle:
            self._goal_handle.cancel_goal_async()
            return "Cancellation request sent for open-vocab grasp behavior."
        return "Open-vocab grasp skill cancellation acknowledged."
