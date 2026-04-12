# Physical Skills Compendium — Design Spec

## Goal

Create a complete set of atomic, code-based physical skill primitives that wrap every capability exposed by the three hardware interfaces (`MobilityInterface`, `ManipulationInterface`, `HeadInterface`). Each skill is a thin, parameterized `Skill` subclass that an LLM agent can invoke directly. Task-level composition is left to the agent or to future higher-level skills.

## Principles

- **One skill = one interface method.** No skill calls two interface methods or combines interfaces.
- **Fully parameterized.** No hardcoded values — the agent supplies all arguments.
- **Consistent pattern.** Every skill follows the same `Skill` subclass structure: declare `Interface` descriptor, implement `name`, `guidelines`, `execute`, `cancel`.
- **Blocking by default.** Skills wait for motion completion (with cancellation polling) so the agent gets a definitive success/failure result. Exceptions: `drive_velocity` and `rotate_in_place` are fire-and-forget with a timed auto-stop (matching their underlying interface), and `head_set_angle` is fire-and-forget (the head servo handles its own interpolation).
- **Read-only skills are valid.** Sensing skills (`arm_get_pose`, `arm_get_load`) return data and perform no actuation.

## Existing Skills (no changes)

These already exist as clean primitives or useful higher-level skills:

| Skill | Type | Interface | Notes |
|---|---|---|---|
| `arm_move_to_xyz` | Atomic | Manipulation | Wraps `move_to_cartesian_pose` |
| `arm_zero_position` | Preset | Manipulation | Convenience — hardcodes `[0,0,0,0,0,0]` |
| `arm_circle_motion` | Demo | Manipulation | Hardcoded circle trajectory |
| `head_emotion` | Composite | Head | Emotion animations via tilt sequences |
| `navigate_to_position` | Atomic | Mobility (Nav2) | Goal-based navigation |
| `navigate_with_vision` | Task-level | Mobility + Vision | Vision-guided navigation |
| `explore_and_find` | Task-level | Mobility + Vision | Roaming + 360° search |
| `scan_for_objects` | Task-level | Mobility + Vision | 360° rotation + Gemini detection |

## New Skills — Mobility (3)

### `drive_velocity`

- **File:** `skills/drive_velocity.py`
- **Wraps:** `MobilityInterface.send_cmd_vel(linear_x, angular_z, duration)`
- **Parameters:**
  - `linear_x: float` — Linear velocity in m/s (positive = forward, negative = backward)
  - `angular_z: float` — Angular velocity in rad/s (positive = counter-clockwise)
  - `duration: float` — Duration in seconds, after which the robot auto-stops
- **Behavior:** Publishes a velocity command, schedules a stop after `duration` seconds, then waits for `duration` before returning success. Non-blocking at the interface level (timer-based stop).
- **Guidelines text:** "Drive the robot at a specified velocity for a duration. Positive linear_x = forward, negative = backward. Positive angular_z = counter-clockwise turn. The robot stops automatically after the duration."

### `rotate_in_place`

- **File:** `skills/rotate_in_place.py`
- **Wraps:** `MobilityInterface.rotate_in_place(angular_speed, duration)`
- **Parameters:**
  - `angular_speed: float` — Angular velocity in rad/s (sign = direction)
  - `duration: float` — Duration in seconds
- **Behavior:** Same fire-and-forget pattern as `drive_velocity` but with zero linear velocity.
- **Guidelines text:** "Rotate the robot in place at a given angular speed for a duration. Positive = counter-clockwise, negative = clockwise. For precise angle-based rotation, use rotate_angle instead."

### `rotate_angle`

- **File:** `skills/rotate_angle.py`
- **Wraps:** `MobilityInterface.rotate(angle_radians)`
- **Parameters:**
  - `angle_radians: float` — Angle to rotate in radians (positive = counter-clockwise)
- **Behavior:** Blocking. Uses Nav2 path planning for precise rotation. Returns success/failure.
- **Guidelines text:** "Rotate the robot by a precise angle using Nav2. Positive = counter-clockwise. This is blocking and precise — use for known angles. For continuous spinning, use rotate_in_place instead."

## New Skills — Manipulation (9)

### `arm_move_to_joints`

- **File:** `skills/arm_move_to_joints.py`
- **Wraps:** `ManipulationInterface.move_to_joint_positions(joint_positions, duration, blocking)`
- **Parameters:**
  - `joint_positions: list[float]` — 6 joint angles in radians
  - `duration: int` — Motion duration in seconds (default 3)
- **Behavior:** Blocking. Sends joint-space command, waits for `duration` with cancellation polling.
- **Guidelines text:** "Move the arm to a specific joint-space configuration. Requires exactly 6 joint angles in radians. Joint 6 is the gripper. Use arm_get_pose to read current position first if needed."

### `arm_follow_trajectory`

- **File:** `skills/arm_follow_trajectory.py`
- **Wraps:** `ManipulationInterface.move_cartesian_trajectory(poses, segment_duration, segment_durations)`
- **Parameters:**
  - `poses: list[dict]` — List of `{x, y, z, roll, pitch, yaw}` waypoints
  - `segment_duration: float` — Time per segment in seconds (default 1.0)
- **Behavior:** Blocking. Solves IK for all waypoints, executes as one smooth trajectory.
- **Guidelines text:** "Move the arm through a sequence of Cartesian waypoints in one smooth motion. Each pose is {x, y, z, roll, pitch, yaw} in meters/radians relative to base_link. Minimum 2 poses required."

### `gripper_open`

- **File:** `skills/gripper_open.py`
- **Wraps:** `ManipulationInterface.open_gripper(percent, duration, blocking)`
- **Parameters:**
  - `percent: float` — How far to open, 0-100% (default 100)
  - `duration: float` — Motion time in seconds (default 0.5)
- **Behavior:** Blocking. Opens gripper to specified percentage, waits for completion.
- **Guidelines text:** "Open the robot gripper. 100% = fully open, 0% = closed. Default is fully open."

### `gripper_close`

- **File:** `skills/gripper_close.py`
- **Wraps:** `ManipulationInterface.close_gripper(strength, duration, blocking)`
- **Parameters:**
  - `strength: float` — Extra closure in radians beyond zero (default 0.0)
  - `duration: float` — Motion time in seconds (default 0.5)
- **Behavior:** Blocking. Closes gripper, optionally with extra squeeze for firmer grasp.
- **Guidelines text:** "Close the robot gripper. Optionally specify strength (radians of extra squeeze beyond closed position) for a firmer grasp."

### `arm_get_pose`

- **File:** `skills/arm_get_pose.py`
- **Wraps:** `ManipulationInterface.get_current_end_effector_pose()`
- **Parameters:** *(none)*
- **Behavior:** Read-only. Returns current end-effector position `{x, y, z}` and orientation `{x, y, z, w}` as a formatted string.
- **Guidelines text:** "Get the current arm end-effector position and orientation in Cartesian space. Returns position (x,y,z in meters) and quaternion orientation relative to base_link. No actuation."

### `arm_get_load`

- **File:** `skills/arm_get_load.py`
- **Wraps:** `ManipulationInterface.get_motor_load()`
- **Parameters:** *(none)*
- **Behavior:** Read-only. Returns 6 motor effort values as a formatted string.
- **Guidelines text:** "Get current motor load/effort for all 6 arm joints. Values are percentages (-100% to 100%). Useful for detecting contact, grasp confirmation, or overload conditions. No actuation."

### `arm_torque_on`

- **File:** `skills/arm_torque_on.py`
- **Wraps:** `ManipulationInterface.torque_on()`
- **Parameters:** *(none)*
- **Behavior:** Blocking. Enables torque on all servos.
- **Guidelines text:** "Enable torque on all arm motors. The arm will hold its current position. Use after arm_torque_off to re-engage the arm."

### `arm_torque_off`

- **File:** `skills/arm_torque_off.py`
- **Wraps:** `ManipulationInterface.torque_off()`
- **Parameters:** *(none)*
- **Behavior:** Blocking. Disables torque — arm goes limp.
- **Guidelines text:** "Disable torque on all arm motors. The arm will go limp and can be manually positioned. Useful for kinesthetic teaching or when the arm is not needed."

### `arm_reboot_servos`

- **File:** `skills/arm_reboot_servos.py`
- **Wraps:** `ManipulationInterface.reboot_servos()`
- **Parameters:** *(none)*
- **Behavior:** Blocking. Reboots all Dynamixel servos to clear hardware errors.
- **Guidelines text:** "Reboot all arm Dynamixel servos. Clears hardware errors and reinitializes motor control. Use when servos are in error state and not responding to commands."

## New Skills — Head (1)

### `head_set_angle`

- **File:** `skills/head_set_angle.py`
- **Wraps:** `HeadInterface.set_position(angle_degrees)`
- **Parameters:**
  - `angle_degrees: int` — Target tilt angle (-25 = full down, +15 = full up, 0 = level)
- **Behavior:** Fire-and-forget. Publishes the angle command immediately.
- **Guidelines text:** "Set the head tilt to a specific angle in degrees. Range: -25 (looking down) to +15 (looking up). 0 = level. Use head_emotion for expressive animations instead."

## File structure after implementation

```
skills/
├── drive_velocity.py          # NEW
├── rotate_in_place.py         # NEW
├── rotate_angle.py            # NEW
├── arm_move_to_joints.py      # NEW
├── arm_follow_trajectory.py   # NEW
├── gripper_open.py            # NEW
├── gripper_close.py           # NEW
├── arm_get_pose.py            # NEW
├── arm_get_load.py            # NEW
├── arm_torque_on.py           # NEW
├── arm_torque_off.py          # NEW
├── arm_reboot_servos.py       # NEW
├── head_set_angle.py          # NEW
├── arm_move_to_xyz.py         # existing
├── arm_zero_position.py       # existing
├── arm_circle_motion.py       # existing
├── head_emotion.py            # existing
├── navigate_to_position.py    # existing
├── ...                        # other existing skills
```

## Implementation pattern

Every new skill follows this template:

```python
#!/usr/bin/env python3
"""<One-line description>."""

import time
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class SkillClassName(Skill):
    """<Docstring>."""

    <interface> = Interface(InterfaceType.<TYPE>)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "<skill_name>"

    def guidelines(self):
        return "<guidelines text>"

    def execute(self, <params>):
        self._cancelled = False
        if self.<interface> is None:
            return "<Interface> not available", SkillResult.FAILURE

        # Call interface method
        # Wait if blocking, with cancellation polling
        # Return (message, SkillResult)

    def cancel(self):
        self._cancelled = True
        return "<Skill> cancelled"
```

## Out of scope

- **`solve_ik`** — implementation detail, not agent-facing
- **`get_joint_limits`** — static data, could be returned as part of guidelines instead
- **Task-level composite skills** (pick-and-place, handover, etc.) — future layer on top of these atomics
- **Changes to existing skills** — no modifications to `arm_move_to_xyz`, `navigate_to_position`, etc.
