# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The model side of a demonstration-conditioned run.

Planning is a fixed ladder rather than a search. The skill decides which frames
the model sees and in what order: each look but the last must be written down
before the next arrives, and the last must produce the phase map. Given a free
choice the model spent every call re-reading nearly identical frames and never
committed a plan, so the ladder guarantees a turn always ends in a decision.

Only the phase map, the notes and the current phase persist between turns.
Which recorded frames to show and which tool may be called are derived per call.
"""

import json
import time
from typing import TYPE_CHECKING

import numpy as np

from innate.demonstration import Demonstration
from innate.imitation_actions import ACTIONS, check_decision

if TYPE_CHECKING:
    from innate.icl_trace import IclTrace


def object_schema(properties):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(properties)}


def tool(name, description, schema):
    return {"type": "function", "name": name, "description": description, "parameters": schema, "strict": True}


PHASE_SCHEMA = object_schema(
    {
        "name": {"type": "string"},
        "start_frame": {"type": "integer"},
        "end_frame": {"type": "integer"},
        "reference_frame": {"type": "integer"},
        "advance_when": {"type": "string"},
    }
)
PLAN_SCHEMA = object_schema({"phases": {"type": "array", "items": PHASE_SCHEMA, "minItems": 2, "maxItems": 6}})


class ImitationPolicy:
    """One run's planner. A subclass supplies `instructions` and `allowed`."""

    instructions = ""
    allowed = tuple(ACTIONS)
    phase_frames = 4  # samples across the current phase's interval, per acting turn
    # Set by the skill for the run's lifetime; None outside a traced run.
    trace: "IclTrace | None" = None

    def __init__(self, demo, chunk_size=1):
        from innate_proxy import ProxyClient

        self.client = ProxyClient()
        if not self.client.is_available():
            raise ValueError("OpenAI access through the Innate proxy is required")
        # Request uncompressed JSON: this proxy path can strip the upstream
        # encoding header, and reading a still-gzipped body fails in json.loads.
        self.client.get_sync_client().headers["Accept-Encoding"] = "identity"
        self.demo = demo
        self.chunk_size = chunk_size
        self.frames = {f["index"]: f for f in demo.frames}
        self.overview = set(self.frames)
        self.phase_map = []
        self.phase = 0
        self.last_trace = []
        # The forced looks the skill hands over are the detailed read that
        # record_phases requires; nothing else may inspect the episode.
        self.inspected_detail = True
        # Ordered (label, indices); the last one is the call that must plan.
        self.looks = [("gripper keyframes", sorted(self.overview))]
        self.notes = []
        self.evidence = ""

    def add_look(self, label, frames):
        """Prepend a look. Its frames stay visible in every later look too, so
        the planning call sees everything that came before it."""
        self.frames.update({f["index"]: f for f in frames})
        self.looks.insert(0, (label, sorted(f["index"] for f in frames)))

    def _load(self, indices):
        """Fetch episode frames not already held. Reading a handful of indices
        costs a fraction of a second; the whole episode would not fit."""
        missing = sorted(set(indices) - set(self.frames))
        if missing:
            extra = Demonstration(self.demo.path, frame_indices=missing, image_time_reference=True)
            self.frames.update({f["index"]: f for f in extra.frames})

    def _acting_context(self):
        """What the episode shows about the phase being executed: that phase
        sampled across its own interval, plus the neighbouring phases' reference
        frames for where it came from and where it leads. One instant per phase
        cannot show how a phase was performed, only that it happened."""
        phase = self.phase_map[self.phase]
        span = np.linspace(phase["start_frame"], phase["end_frame"], self.phase_frames, dtype=int)
        picked = {int(i) for i in span} | {phase["reference_frame"]}
        for neighbour in (self.phase - 1, self.phase + 1):
            if 0 <= neighbour < len(self.phase_map):
                picked.add(self.phase_map[neighbour]["reference_frame"])
        self._load(picked)
        # Bound retained images: keep the looks, every phase anchor, and this selection.
        keep = picked | {p["reference_frame"] for p in self.phase_map}
        keep |= {i for _, indices in self.looks for i in indices}
        self.frames = {i: f for i, f in self.frames.items() if i in keep}
        return picked

    def _context(self, indices):
        result = []
        for i in sorted(indices):
            f = self.frames[i]
            result.append(
                {"type": "input_text", "text": "DEMO " + json.dumps({k: v for k, v in f.items() if k != "images"})}
            )
            for name, image in f["images"].items():
                result.extend(
                    [
                        {"type": "input_text", "text": name},
                        {"type": "input_image", "image_url": "data:image/jpeg;base64," + image},
                    ]
                )
        return result

    def _act_tool(self):
        return {
            "type": "function",
            "name": "act",
            "strict": True,
            "description": "Return a chunk of joint movements, or one decision of any other kind.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["phase", "previous_phase_complete", "evidence", "actions"],
                "properties": {
                    "phase": {"type": "integer", "description": "The phase you are acting in, from the recorded map."},
                    "previous_phase_complete": {
                        "type": "boolean",
                        "description": "Only when moving on: its advance_when is visibly satisfied.",
                    },
                    "evidence": {"type": "string", "description": "The live evidence for that phase choice."},
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": self.chunk_size,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["action", "pose", "reason"],
                            "properties": {
                                "action": {"type": "string", "enum": sorted(self.allowed)},
                                "pose": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "maxItems": 6,
                                    "description": "joint_step [joint, delta]; move [x,y,z,r,p,y]; base_step [m]; else [].",
                                },
                                "reason": {"type": "string", "minLength": 1, "maxLength": 240},
                            },
                        },
                    },
                },
            },
        }

    def _note_tool(self):
        return {
            "type": "function",
            "name": "note_observations",
            "strict": True,
            "description": "Write down what this set of frames shows, before the next set arrives.",
            "parameters": object_schema({"observations": {"type": "string"}}),
        }

    def _phases_tool(self):
        return tool(
            "record_phases",
            "Once per run, record the visually inferred task phases and observable completion conditions.",
            PLAN_SCHEMA,
        )

    def _request(self, content, tool_name):
        by_name = {
            "act": self._act_tool(),
            "note_observations": self._note_tool(),
            "record_phases": self._phases_tool(),
        }
        body = {
            "model": "gpt-6-astra",
            "service_tier": "priority",
            "store": False,
            "reasoning": {"effort": "low"},
            "instructions": self.instructions + f"\nFor this run chunk_size={self.chunk_size}.",
            "tools": [by_name[tool_name]],
            "tool_choice": {"type": "function", "name": tool_name},
            "parallel_tool_calls": False,
            "max_output_tokens": 2500,
            "input": [{"role": "user", "content": content}],
        }
        with self.client.request_stream("openai", "/v1/responses", method="POST", json=body, timeout=40) as response:
            response.raise_for_status()
            value = json.loads(response.read())
        calls = [item for item in value.get("output", []) if item.get("type") == "function_call"]
        if value.get("status") != "completed" or len(calls) != 1:
            raise ValueError("Expected one completed demonstration tool call")
        return calls[0]["name"], json.loads(calls[0]["arguments"])

    def _call(self, observation, history, indices, tool, label):
        content = self._context(indices)
        content.append(
            {
                "type": "input_text",
                "text": json.dumps(
                    {
                        "look": label,
                        "frames_shown": sorted(indices),
                        "episode_frames": len(self.demo.poses),
                        "phase_map": self.phase_map,
                        "current_phase": self.phase,
                        "notes_so_far": self.notes,
                        "live": {k: v for k, v in observation.items() if k != "images"},
                        "history": history[-6:],
                    }
                ),
            }
        )
        for name, image in observation["images"].items():
            content += [
                {"type": "input_text", "text": "LIVE " + name},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + image},
            ]
        started = time.monotonic()
        name, args = self._request(content, tool)
        latency = time.monotonic() - started
        self.last_trace.append({"tool": name, "arguments": args, "look": label})
        if self.trace:
            self.trace.tool(name, {"look": label, "frames_shown": len(indices), **args}, latency)
        if name == "note_observations":
            self.notes.append({"look": label, "text": args["observations"]})
            return None
        if name == "record_phases":
            self._record_phases(args)
            if self.trace:
                self.trace.phases(self.phase_map)
            return None
        return self._action(args, history)

    def decide(self, observation, history):
        self.last_trace = []
        if not self.phase_map:
            self.notes = []
            shown = set()
            for index, (label, indices) in enumerate(self.looks):
                shown |= set(indices)
                planning = index == len(self.looks) - 1
                self._call(observation, history, shown, "record_phases" if planning else "note_observations", label)
        return self._call(observation, history, self._acting_context(), "act", "live")

    def _record_phases(self, args):
        if self.phase_map or not self.inspected_detail:
            raise ValueError("Inspect detailed frames before recording a single phase map")
        phases = args.get("phases")
        if not isinstance(phases, list) or not 2 <= len(phases) <= 6:
            raise ValueError("Expected two to six phases")
        previous_start = -1
        previous_end = -1
        for phase in phases:
            if set(phase) != set(PHASE_SCHEMA["properties"]):
                raise ValueError("Invalid phase fields")
            start, end, ref = (phase[k] for k in ("start_frame", "end_frame", "reference_frame"))
            if any(type(i) is not int for i in (start, end, ref)) or not 0 <= start <= ref <= end < len(
                self.demo.poses
            ):
                raise ValueError("Invalid phase interval")
            if start < previous_start or end < previous_end or ref not in self.frames:
                raise ValueError("Phases must be ordered and reference inspected frames")
            if any(not isinstance(phase[k], str) or not 1 <= len(phase[k]) <= 600 for k in ("name", "advance_when")):
                raise ValueError("Phase needs a name and completion condition")
            previous_start, previous_end = start, end
        self.phase_map = phases

    def _action(self, args, history):
        if not isinstance(args, dict) or set(args) != {"phase", "previous_phase_complete", "evidence", "actions"}:
            raise ValueError("act requires phase, previous_phase_complete, evidence and actions")
        phase = args["phase"]
        if type(phase) is not int or phase not in (self.phase, self.phase + 1) or phase >= len(self.phase_map):
            raise ValueError("Cannot skip phases")
        if type(args["previous_phase_complete"]) is not bool:
            raise ValueError("Invalid completion assessment")
        if phase != self.phase and (not history or not args["previous_phase_complete"]):
            raise ValueError("Advancing needs a prior observation and completion evidence")
        if not isinstance(args["evidence"], str) or not 1 <= len(args["evidence"]) <= 1200:
            raise ValueError("Live phase evidence required")
        values = args["actions"]
        if not isinstance(values, list) or not 1 <= len(values) <= self.chunk_size:
            raise ValueError("Decision batch exceeds chunk_size")
        checked = [check_decision(v, self.allowed) for v in values]
        if len(checked) > 1 and any(v["action"] != "joint_step" for v in checked):
            raise ValueError("Only joint movements can be batched")
        if any(v["action"] == "done" for v in checked) and phase != len(self.phase_map) - 1:
            raise ValueError("Cannot finish before the final phase")
        self.phase = phase
        self.evidence = args["evidence"]
        return checked

    def restart_attempt(self):
        """A miss changes the live approach, not the learned demonstration."""
        self.phase = 0
