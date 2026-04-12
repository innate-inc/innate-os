# Physical Skills Compendium Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 13 atomic code-based physical skill primitives that wrap every unwrapped capability in MobilityInterface, ManipulationInterface, and HeadInterface.

**Architecture:** Each skill is a thin `Skill` subclass (20-40 lines) that declares an `Interface` descriptor, validates it's available, calls exactly one interface method, and returns a `(message, SkillResult)` tuple. Tests mock the interface and verify correct delegation.

**Tech Stack:** Python 3, `brain_client.skill_types` (Skill, SkillResult, Interface, InterfaceType), unittest with mocks. ROS dependencies are mocked at module level for testing.

**Spec:** `docs/superpowers/specs/2026-04-11-physical-skills-compendium-design.md`

---

### Task 1: Test Infrastructure

**Files:**
- Create: `skills/tests/__init__.py`
- Create: `skills/tests/conftest.py`

- [ ] **Step 1: Create test package and ROS mock conftest**

Create `skills/tests/__init__.py` (empty) and `skills/tests/conftest.py` that mocks ROS dependencies so skill modules can be imported without a live ROS environment:

```python
# skills/tests/__init__.py
```

```python
# skills/tests/conftest.py
"""Mock ROS dependencies for skill unit tests."""

import sys
from unittest.mock import MagicMock

# Mock all ROS packages before any skill imports
for mod in [
    "rclpy",
    "rclpy.node",
    "rclpy.action",
    "action_msgs",
    "action_msgs.msg",
    "geometry_msgs",
    "geometry_msgs.msg",
    "nav2_simple_commander",
    "nav2_simple_commander.robot_navigator",
    "sensor_msgs",
    "sensor_msgs.msg",
    "std_msgs",
    "std_msgs.msg",
    "std_srvs",
    "std_srvs.srv",
    "maurice_msgs",
    "maurice_msgs.srv",
    "brain_messages",
    "brain_messages.action",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
```

- [ ] **Step 2: Verify conftest loads**

Run:
```bash
cd /root/innate-os && python -c "import skills.tests.conftest; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add skills/tests/__init__.py skills/tests/conftest.py
git commit -m "test: add ROS mock infrastructure for skill unit tests"
```

---

### Task 2: Mobility Skills — drive_velocity, rotate_in_place, rotate_angle

**Files:**
- Create: `skills/drive_velocity.py`
- Create: `skills/rotate_in_place.py`
- Create: `skills/rotate_angle.py`
- Create: `skills/tests/test_mobility_skills.py`

- [ ] **Step 1: Write failing tests for all 3 mobility skills**

Create `skills/tests/test_mobility_skills.py`:

```python
#!/usr/bin/env python3
"""Tests for mobility atomic skills."""

import skills.tests.conftest  # noqa: F401 — must be imported before skill modules
import time
import unittest
from unittest.mock import MagicMock, patch

from brain_client.skill_types import SkillResult


class TestDriveVelocity(unittest.TestCase):
    def setUp(self):
        from skills.drive_velocity import DriveVelocity
        self.skill = DriveVelocity(logger=MagicMock())
        mock_mobility = MagicMock()
        self.skill.inject_interface(
            __import__("brain_client.skill_types", fromlist=["InterfaceType"]).InterfaceType.MOBILITY,
            mock_mobility,
        )
        self.mock_mobility = mock_mobility

    @patch("time.sleep")
    def test_execute_calls_send_cmd_vel(self, mock_sleep):
        msg, result = self.skill.execute(linear_x=0.5, angular_z=0.1, duration=2.0)
        self.mock_mobility.send_cmd_vel.assert_called_once_with(
            linear_x=0.5, angular_z=0.1, duration=2.0
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_fails_without_interface(self):
        from skills.drive_velocity import DriveVelocity
        skill = DriveVelocity(logger=MagicMock())
        msg, result = skill.execute(linear_x=0.5, angular_z=0.0, duration=1.0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "drive_velocity")

    def test_guidelines_not_empty(self):
        self.assertIsNotNone(self.skill.guidelines())
        self.assertGreater(len(self.skill.guidelines()), 0)


class TestRotateInPlace(unittest.TestCase):
    def setUp(self):
        from skills.rotate_in_place import RotateInPlace
        self.skill = RotateInPlace(logger=MagicMock())
        mock_mobility = MagicMock()
        self.skill.inject_interface(
            __import__("brain_client.skill_types", fromlist=["InterfaceType"]).InterfaceType.MOBILITY,
            mock_mobility,
        )
        self.mock_mobility = mock_mobility

    @patch("time.sleep")
    def test_execute_calls_rotate_in_place(self, mock_sleep):
        msg, result = self.skill.execute(angular_speed=1.0, duration=3.0)
        self.mock_mobility.rotate_in_place.assert_called_once_with(
            angular_speed=1.0, duration=3.0
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_fails_without_interface(self):
        from skills.rotate_in_place import RotateInPlace
        skill = RotateInPlace(logger=MagicMock())
        msg, result = skill.execute(angular_speed=1.0, duration=1.0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "rotate_in_place")


class TestRotateAngle(unittest.TestCase):
    def setUp(self):
        from skills.rotate_angle import RotateAngle
        self.skill = RotateAngle(logger=MagicMock())
        mock_mobility = MagicMock()
        self.skill.inject_interface(
            __import__("brain_client.skill_types", fromlist=["InterfaceType"]).InterfaceType.MOBILITY,
            mock_mobility,
        )
        self.mock_mobility = mock_mobility

    def test_execute_calls_rotate(self):
        self.mock_mobility.rotate.return_value = True
        msg, result = self.skill.execute(angle_radians=1.57)
        self.mock_mobility.rotate.assert_called_once_with(1.57)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_when_rotate_fails(self):
        self.mock_mobility.rotate.return_value = False
        msg, result = self.skill.execute(angle_radians=1.57)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.rotate_angle import RotateAngle
        skill = RotateAngle(logger=MagicMock())
        msg, result = skill.execute(angle_radians=1.0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "rotate_angle")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_mobility_skills.py -v
```
Expected: FAIL — `ModuleNotFoundError` for `skills.drive_velocity`, `skills.rotate_in_place`, `skills.rotate_angle`

- [ ] **Step 3: Implement drive_velocity.py**

Create `skills/drive_velocity.py`:

```python
#!/usr/bin/env python3
"""Drive the robot at a specified velocity for a duration."""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class DriveVelocity(Skill):
    """Drive at a linear/angular velocity for a specified duration, then stop."""

    mobility = Interface(InterfaceType.MOBILITY)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "drive_velocity"

    def guidelines(self):
        return (
            "Drive the robot at a specified velocity for a duration. "
            "Positive linear_x = forward, negative = backward. "
            "Positive angular_z = counter-clockwise turn. "
            "The robot stops automatically after the duration."
        )

    def execute(self, linear_x: float, angular_z: float = 0.0, duration: float = 1.0):
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        self.logger.info(
            f"Driving at linear_x={linear_x}, angular_z={angular_z} for {duration}s"
        )

        self.mobility.send_cmd_vel(linear_x=linear_x, angular_z=angular_z, duration=duration)

        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                return "Drive cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        return f"Drove at ({linear_x}, {angular_z}) for {duration}s", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Drive velocity cancelled"
```

- [ ] **Step 4: Implement rotate_in_place.py**

Create `skills/rotate_in_place.py`:

```python
#!/usr/bin/env python3
"""Rotate the robot in place at a given angular speed for a duration."""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class RotateInPlace(Skill):
    """Rotate in place with specified angular speed for a duration."""

    mobility = Interface(InterfaceType.MOBILITY)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "rotate_in_place"

    def guidelines(self):
        return (
            "Rotate the robot in place at a given angular speed for a duration. "
            "Positive = counter-clockwise, negative = clockwise. "
            "For precise angle-based rotation, use rotate_angle instead."
        )

    def execute(self, angular_speed: float, duration: float = 1.0):
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        self.logger.info(f"Rotating in place at {angular_speed} rad/s for {duration}s")

        self.mobility.rotate_in_place(angular_speed=angular_speed, duration=duration)

        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                return "Rotation cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        return f"Rotated at {angular_speed} rad/s for {duration}s", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Rotate in place cancelled"
```

- [ ] **Step 5: Implement rotate_angle.py**

Create `skills/rotate_angle.py`:

```python
#!/usr/bin/env python3
"""Rotate the robot by a precise angle using Nav2."""

import math

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class RotateAngle(Skill):
    """Rotate in place by a specific angle using Nav2 (blocking)."""

    mobility = Interface(InterfaceType.MOBILITY)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "rotate_angle"

    def guidelines(self):
        return (
            "Rotate the robot by a precise angle using Nav2. "
            "Positive = counter-clockwise. This is blocking and precise — "
            "use for known angles. For continuous spinning, use rotate_in_place instead."
        )

    def execute(self, angle_radians: float):
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        self.logger.info(f"Rotating {math.degrees(angle_radians):.1f}° via Nav2")

        success = self.mobility.rotate(angle_radians)

        if not success:
            return "Rotation failed", SkillResult.FAILURE

        return f"Rotated {math.degrees(angle_radians):.1f}°", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Rotate angle cancelled"
```

- [ ] **Step 6: Run tests to verify they pass**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_mobility_skills.py -v
```
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add skills/drive_velocity.py skills/rotate_in_place.py skills/rotate_angle.py skills/tests/test_mobility_skills.py
git commit -m "feat: add mobility atomic skills (drive_velocity, rotate_in_place, rotate_angle)"
```

---

### Task 3: Gripper Skills — gripper_open, gripper_close

**Files:**
- Create: `skills/gripper_open.py`
- Create: `skills/gripper_close.py`
- Create: `skills/tests/test_gripper_skills.py`

- [ ] **Step 1: Write failing tests for gripper skills**

Create `skills/tests/test_gripper_skills.py`:

```python
#!/usr/bin/env python3
"""Tests for gripper atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestGripperOpen(unittest.TestCase):
    def setUp(self):
        from skills.gripper_open import GripperOpen
        self.skill = GripperOpen(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_open_gripper_default(self):
        self.mock_manip.open_gripper.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.open_gripper.assert_called_once_with(
            percent=100.0, duration=0.5, blocking=True
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_calls_open_gripper_custom(self):
        self.mock_manip.open_gripper.return_value = True
        msg, result = self.skill.execute(percent=50.0, duration=1.0)
        self.mock_manip.open_gripper.assert_called_once_with(
            percent=50.0, duration=1.0, blocking=True
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.open_gripper.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.gripper_open import GripperOpen
        skill = GripperOpen(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "gripper_open")


class TestGripperClose(unittest.TestCase):
    def setUp(self):
        from skills.gripper_close import GripperClose
        self.skill = GripperClose(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_close_gripper_default(self):
        self.mock_manip.close_gripper.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.close_gripper.assert_called_once_with(
            strength=0.0, duration=0.5, blocking=True
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_calls_close_gripper_custom(self):
        self.mock_manip.close_gripper.return_value = True
        msg, result = self.skill.execute(strength=0.1, duration=1.0)
        self.mock_manip.close_gripper.assert_called_once_with(
            strength=0.1, duration=1.0, blocking=True
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.close_gripper.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.gripper_close import GripperClose
        skill = GripperClose(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "gripper_close")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_gripper_skills.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement gripper_open.py**

Create `skills/gripper_open.py`:

```python
#!/usr/bin/env python3
"""Open the robot gripper to a specified percentage."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class GripperOpen(Skill):
    """Open the gripper to a specified percentage (0-100%)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "gripper_open"

    def guidelines(self):
        return (
            "Open the robot gripper. 100% = fully open, 0% = closed. "
            "Default is fully open."
        )

    def execute(self, percent: float = 100.0, duration: float = 0.5):
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info(f"Opening gripper to {percent}%")

        success = self.manipulation.open_gripper(
            percent=percent, duration=duration, blocking=True
        )

        if not success:
            return "Failed to open gripper", SkillResult.FAILURE

        return f"Gripper opened to {percent}%", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gripper open cancelled"
```

- [ ] **Step 4: Implement gripper_close.py**

Create `skills/gripper_close.py`:

```python
#!/usr/bin/env python3
"""Close the robot gripper."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class GripperClose(Skill):
    """Close the gripper, optionally with extra squeeze for firmer grasp."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "gripper_close"

    def guidelines(self):
        return (
            "Close the robot gripper. Optionally specify strength (radians of "
            "extra squeeze beyond closed position) for a firmer grasp."
        )

    def execute(self, strength: float = 0.0, duration: float = 0.5):
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info(f"Closing gripper with strength={strength}")

        success = self.manipulation.close_gripper(
            strength=strength, duration=duration, blocking=True
        )

        if not success:
            return "Failed to close gripper", SkillResult.FAILURE

        return "Gripper closed", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gripper close cancelled"
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_gripper_skills.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add skills/gripper_open.py skills/gripper_close.py skills/tests/test_gripper_skills.py
git commit -m "feat: add gripper atomic skills (gripper_open, gripper_close)"
```

---

### Task 4: Arm Sensing Skills — arm_get_pose, arm_get_load

**Files:**
- Create: `skills/arm_get_pose.py`
- Create: `skills/arm_get_load.py`
- Create: `skills/tests/test_arm_sensing_skills.py`

- [ ] **Step 1: Write failing tests for arm sensing skills**

Create `skills/tests/test_arm_sensing_skills.py`:

```python
#!/usr/bin/env python3
"""Tests for arm sensing atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestArmGetPose(unittest.TestCase):
    def setUp(self):
        from skills.arm_get_pose import ArmGetPose
        self.skill = ArmGetPose(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_returns_pose(self):
        self.mock_manip.get_current_end_effector_pose.return_value = {
            "position": {"x": 0.2, "y": 0.0, "z": 0.3},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            "frame_id": "base_link",
        }
        msg, result = self.skill.execute()
        self.mock_manip.get_current_end_effector_pose.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)
        self.assertIn("0.2", msg)

    def test_execute_returns_failure_when_no_pose(self):
        self.mock_manip.get_current_end_effector_pose.return_value = None
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_get_pose import ArmGetPose
        skill = ArmGetPose(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_get_pose")


class TestArmGetLoad(unittest.TestCase):
    def setUp(self):
        from skills.arm_get_load import ArmGetLoad
        self.skill = ArmGetLoad(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_returns_load(self):
        self.mock_manip.get_motor_load.return_value = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        msg, result = self.skill.execute()
        self.mock_manip.get_motor_load.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)
        self.assertIn("1.0", msg)

    def test_execute_returns_failure_when_no_load(self):
        self.mock_manip.get_motor_load.return_value = None
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_get_load import ArmGetLoad
        skill = ArmGetLoad(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_get_load")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_arm_sensing_skills.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement arm_get_pose.py**

Create `skills/arm_get_pose.py`:

```python
#!/usr/bin/env python3
"""Get the current arm end-effector position and orientation."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmGetPose(Skill):
    """Query current end-effector pose in Cartesian space (read-only, no actuation)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_get_pose"

    def guidelines(self):
        return (
            "Get the current arm end-effector position and orientation in "
            "Cartesian space. Returns position (x,y,z in meters) and quaternion "
            "orientation relative to base_link. No actuation."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        pose = self.manipulation.get_current_end_effector_pose()

        if pose is None:
            return "No end-effector pose available", SkillResult.FAILURE

        pos = pose["position"]
        ori = pose["orientation"]
        msg = (
            f"End-effector pose — "
            f"position: x={pos['x']:.4f}, y={pos['y']:.4f}, z={pos['z']:.4f} | "
            f"orientation: x={ori['x']:.4f}, y={ori['y']:.4f}, z={ori['z']:.4f}, w={ori['w']:.4f}"
        )
        return msg, SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel (read-only skill)"
```

- [ ] **Step 4: Implement arm_get_load.py**

Create `skills/arm_get_load.py`:

```python
#!/usr/bin/env python3
"""Get current motor load/effort for all arm joints."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmGetLoad(Skill):
    """Query current motor effort values for all 6 joints (read-only, no actuation)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_get_load"

    def guidelines(self):
        return (
            "Get current motor load/effort for all 6 arm joints. "
            "Values are percentages (-100% to 100%). Useful for detecting contact, "
            "grasp confirmation, or overload conditions. No actuation."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        loads = self.manipulation.get_motor_load()

        if loads is None:
            return "No motor load data available", SkillResult.FAILURE

        formatted = ", ".join(f"j{i+1}={v:.1f}%" for i, v in enumerate(loads))
        return f"Motor loads: {formatted}", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel (read-only skill)"
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_arm_sensing_skills.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add skills/arm_get_pose.py skills/arm_get_load.py skills/tests/test_arm_sensing_skills.py
git commit -m "feat: add arm sensing skills (arm_get_pose, arm_get_load)"
```

---

### Task 5: Arm Servo Control Skills — arm_torque_on, arm_torque_off, arm_reboot_servos

**Files:**
- Create: `skills/arm_torque_on.py`
- Create: `skills/arm_torque_off.py`
- Create: `skills/arm_reboot_servos.py`
- Create: `skills/tests/test_arm_servo_skills.py`

- [ ] **Step 1: Write failing tests for servo control skills**

Create `skills/tests/test_arm_servo_skills.py`:

```python
#!/usr/bin/env python3
"""Tests for arm servo control atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestArmTorqueOn(unittest.TestCase):
    def setUp(self):
        from skills.arm_torque_on import ArmTorqueOn
        self.skill = ArmTorqueOn(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_torque_on(self):
        self.mock_manip.torque_on.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.torque_on.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure(self):
        self.mock_manip.torque_on.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_torque_on import ArmTorqueOn
        skill = ArmTorqueOn(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_torque_on")


class TestArmTorqueOff(unittest.TestCase):
    def setUp(self):
        from skills.arm_torque_off import ArmTorqueOff
        self.skill = ArmTorqueOff(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_torque_off(self):
        self.mock_manip.torque_off.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.torque_off.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure(self):
        self.mock_manip.torque_off.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_torque_off import ArmTorqueOff
        skill = ArmTorqueOff(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_torque_off")


class TestArmRebootServos(unittest.TestCase):
    def setUp(self):
        from skills.arm_reboot_servos import ArmRebootServos
        self.skill = ArmRebootServos(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_reboot(self):
        self.mock_manip.reboot_servos.return_value = True
        msg, result = self.skill.execute()
        self.mock_manip.reboot_servos.assert_called_once()
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure(self):
        self.mock_manip.reboot_servos.return_value = False
        msg, result = self.skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_fails_without_interface(self):
        from skills.arm_reboot_servos import ArmRebootServos
        skill = ArmRebootServos(logger=MagicMock())
        msg, result = skill.execute()
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_reboot_servos")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_arm_servo_skills.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement arm_torque_on.py**

Create `skills/arm_torque_on.py`:

```python
#!/usr/bin/env python3
"""Enable torque on all arm motors."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmTorqueOn(Skill):
    """Enable torque on all arm servos so the arm holds its position."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_torque_on"

    def guidelines(self):
        return (
            "Enable torque on all arm motors. The arm will hold its current "
            "position. Use after arm_torque_off to re-engage the arm."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info("Enabling arm torque")

        success = self.manipulation.torque_on()

        if not success:
            return "Failed to enable torque", SkillResult.FAILURE

        return "Arm torque enabled", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel"
```

- [ ] **Step 4: Implement arm_torque_off.py**

Create `skills/arm_torque_off.py`:

```python
#!/usr/bin/env python3
"""Disable torque on all arm motors."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmTorqueOff(Skill):
    """Disable torque on all arm servos — arm goes limp."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_torque_off"

    def guidelines(self):
        return (
            "Disable torque on all arm motors. The arm will go limp and "
            "can be manually positioned. Useful for kinesthetic teaching "
            "or when the arm is not needed."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info("Disabling arm torque")

        success = self.manipulation.torque_off()

        if not success:
            return "Failed to disable torque", SkillResult.FAILURE

        return "Arm torque disabled", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel"
```

- [ ] **Step 5: Implement arm_reboot_servos.py**

Create `skills/arm_reboot_servos.py`:

```python
#!/usr/bin/env python3
"""Reboot all arm Dynamixel servos."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmRebootServos(Skill):
    """Reboot all Dynamixel servos to clear hardware errors."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_reboot_servos"

    def guidelines(self):
        return (
            "Reboot all arm Dynamixel servos. Clears hardware errors and "
            "reinitializes motor control. Use when servos are in error state "
            "and not responding to commands."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info("Rebooting arm servos")

        success = self.manipulation.reboot_servos()

        if not success:
            return "Failed to reboot servos", SkillResult.FAILURE

        return "Arm servos rebooted", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel"
```

- [ ] **Step 6: Run tests to verify they pass**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_arm_servo_skills.py -v
```
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add skills/arm_torque_on.py skills/arm_torque_off.py skills/arm_reboot_servos.py skills/tests/test_arm_servo_skills.py
git commit -m "feat: add arm servo control skills (arm_torque_on, arm_torque_off, arm_reboot_servos)"
```

---

### Task 6: Arm Movement Skills — arm_move_to_joints, arm_follow_trajectory

**Files:**
- Create: `skills/arm_move_to_joints.py`
- Create: `skills/arm_follow_trajectory.py`
- Create: `skills/tests/test_arm_movement_skills.py`

- [ ] **Step 1: Write failing tests for arm movement skills**

Create `skills/tests/test_arm_movement_skills.py`:

```python
#!/usr/bin/env python3
"""Tests for arm movement atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock, patch

from brain_client.skill_types import InterfaceType, SkillResult


class TestArmMoveToJoints(unittest.TestCase):
    def setUp(self):
        from skills.arm_move_to_joints import ArmMoveToJoints
        self.skill = ArmMoveToJoints(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    @patch("time.sleep")
    def test_execute_calls_move_to_joint_positions(self, mock_sleep):
        self.mock_manip.move_to_joint_positions.return_value = True
        joints = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        msg, result = self.skill.execute(joint_positions=joints, duration=3)
        self.mock_manip.move_to_joint_positions.assert_called_once_with(
            joint_positions=joints, duration=3, blocking=False
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.move_to_joint_positions.return_value = False
        joints = [0.0] * 6
        msg, result = self.skill.execute(joint_positions=joints)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_rejects_wrong_joint_count(self):
        joints = [0.0, 0.0, 0.0]
        msg, result = self.skill.execute(joint_positions=joints)
        self.assertEqual(result, SkillResult.FAILURE)
        self.mock_manip.move_to_joint_positions.assert_not_called()

    def test_execute_fails_without_interface(self):
        from skills.arm_move_to_joints import ArmMoveToJoints
        skill = ArmMoveToJoints(logger=MagicMock())
        msg, result = skill.execute(joint_positions=[0.0] * 6)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_move_to_joints")


class TestArmFollowTrajectory(unittest.TestCase):
    def setUp(self):
        from skills.arm_follow_trajectory import ArmFollowTrajectory
        self.skill = ArmFollowTrajectory(logger=MagicMock())
        mock_manip = MagicMock()
        self.skill.inject_interface(InterfaceType.MANIPULATION, mock_manip)
        self.mock_manip = mock_manip

    def test_execute_calls_move_cartesian_trajectory(self):
        self.mock_manip.move_cartesian_trajectory.return_value = True
        poses = [
            {"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            {"x": 0.3, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        ]
        msg, result = self.skill.execute(poses=poses, segment_duration=1.0)
        self.mock_manip.move_cartesian_trajectory.assert_called_once_with(
            poses=poses, segment_duration=1.0
        )
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_returns_failure_on_interface_error(self):
        self.mock_manip.move_cartesian_trajectory.return_value = False
        poses = [
            {"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            {"x": 0.3, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        ]
        msg, result = self.skill.execute(poses=poses)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_execute_rejects_fewer_than_2_poses(self):
        poses = [{"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}]
        msg, result = self.skill.execute(poses=poses)
        self.assertEqual(result, SkillResult.FAILURE)
        self.mock_manip.move_cartesian_trajectory.assert_not_called()

    def test_execute_fails_without_interface(self):
        from skills.arm_follow_trajectory import ArmFollowTrajectory
        skill = ArmFollowTrajectory(logger=MagicMock())
        poses = [
            {"x": 0.2, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            {"x": 0.3, "y": 0.0, "z": 0.3, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        ]
        msg, result = skill.execute(poses=poses)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "arm_follow_trajectory")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_arm_movement_skills.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement arm_move_to_joints.py**

Create `skills/arm_move_to_joints.py`:

```python
#!/usr/bin/env python3
"""Move arm to a specific joint-space configuration."""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmMoveToJoints(Skill):
    """Move the arm to arbitrary joint positions (6 joints in radians)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "arm_move_to_joints"

    def guidelines(self):
        return (
            "Move the arm to a specific joint-space configuration. "
            "Requires exactly 6 joint angles in radians. "
            "Joint 6 is the gripper. Use arm_get_pose to read current position first if needed."
        )

    def execute(self, joint_positions: list[float], duration: int = 3):
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        if len(joint_positions) != 6:
            return f"Expected 6 joint positions, got {len(joint_positions)}", SkillResult.FAILURE

        self.logger.info(f"Moving arm to joints {joint_positions} over {duration}s")

        success = self.manipulation.move_to_joint_positions(
            joint_positions=joint_positions, duration=duration, blocking=False
        )

        if not success:
            return "Failed to send joint position command", SkillResult.FAILURE

        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        return f"Arm moved to joint positions {joint_positions}", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Arm move to joints cancelled"
```

- [ ] **Step 4: Implement arm_follow_trajectory.py**

Create `skills/arm_follow_trajectory.py`:

```python
#!/usr/bin/env python3
"""Move arm through a sequence of Cartesian waypoints."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmFollowTrajectory(Skill):
    """Move the arm through Cartesian waypoints in one smooth trajectory."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "arm_follow_trajectory"

    def guidelines(self):
        return (
            "Move the arm through a sequence of Cartesian waypoints in one smooth motion. "
            "Each pose is {x, y, z, roll, pitch, yaw} in meters/radians relative to base_link. "
            "Minimum 2 poses required."
        )

    def execute(self, poses: list[dict], segment_duration: float = 1.0):
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        if len(poses) < 2:
            return "Need at least 2 poses for a trajectory", SkillResult.FAILURE

        self.logger.info(f"Following trajectory with {len(poses)} waypoints")

        success = self.manipulation.move_cartesian_trajectory(
            poses=poses, segment_duration=segment_duration
        )

        if not success:
            return "Trajectory execution failed", SkillResult.FAILURE

        return f"Completed trajectory with {len(poses)} waypoints", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Arm trajectory cancelled"
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_arm_movement_skills.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add skills/arm_move_to_joints.py skills/arm_follow_trajectory.py skills/tests/test_arm_movement_skills.py
git commit -m "feat: add arm movement skills (arm_move_to_joints, arm_follow_trajectory)"
```

---

### Task 7: Head Skill — head_set_angle

**Files:**
- Create: `skills/head_set_angle.py`
- Create: `skills/tests/test_head_skills.py`

- [ ] **Step 1: Write failing tests for head_set_angle**

Create `skills/tests/test_head_skills.py`:

```python
#!/usr/bin/env python3
"""Tests for head atomic skills."""

import skills.tests.conftest  # noqa: F401
import unittest
from unittest.mock import MagicMock

from brain_client.skill_types import InterfaceType, SkillResult


class TestHeadSetAngle(unittest.TestCase):
    def setUp(self):
        from skills.head_set_angle import HeadSetAngle
        self.skill = HeadSetAngle(logger=MagicMock())
        mock_head = MagicMock()
        self.skill.inject_interface(InterfaceType.HEAD, mock_head)
        self.mock_head = mock_head

    def test_execute_calls_set_position(self):
        msg, result = self.skill.execute(angle_degrees=-10)
        self.mock_head.set_position.assert_called_once_with(-10)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_clamps_to_valid_range(self):
        msg, result = self.skill.execute(angle_degrees=-50)
        self.mock_head.set_position.assert_called_once_with(-25)
        self.assertEqual(result, SkillResult.SUCCESS)

        self.mock_head.reset_mock()
        msg, result = self.skill.execute(angle_degrees=30)
        self.mock_head.set_position.assert_called_once_with(15)
        self.assertEqual(result, SkillResult.SUCCESS)

    def test_execute_fails_without_interface(self):
        from skills.head_set_angle import HeadSetAngle
        skill = HeadSetAngle(logger=MagicMock())
        msg, result = skill.execute(angle_degrees=0)
        self.assertEqual(result, SkillResult.FAILURE)

    def test_name(self):
        self.assertEqual(self.skill.name, "head_set_angle")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_head_skills.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement head_set_angle.py**

Create `skills/head_set_angle.py`:

```python
#!/usr/bin/env python3
"""Set the head tilt to a specific angle."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult

HEAD_MIN_ANGLE = -25
HEAD_MAX_ANGLE = 15


class HeadSetAngle(Skill):
    """Set the head tilt to a specific angle in degrees."""

    head = Interface(InterfaceType.HEAD)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "head_set_angle"

    def guidelines(self):
        return (
            "Set the head tilt to a specific angle in degrees. "
            f"Range: {HEAD_MIN_ANGLE} (looking down) to {HEAD_MAX_ANGLE} (looking up). "
            "0 = level. Use head_emotion for expressive animations instead."
        )

    def execute(self, angle_degrees: int):
        if self.head is None:
            return "Head interface not available", SkillResult.FAILURE

        angle_degrees = int(max(HEAD_MIN_ANGLE, min(HEAD_MAX_ANGLE, angle_degrees)))

        self.logger.info(f"Setting head angle to {angle_degrees}°")
        self.head.set_position(angle_degrees)

        return f"Head set to {angle_degrees}°", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel (fire-and-forget)"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/test_head_skills.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add skills/head_set_angle.py skills/tests/test_head_skills.py
git commit -m "feat: add head_set_angle skill"
```

---

### Task 8: Full Test Suite Verification

- [ ] **Step 1: Run all skill tests**

Run:
```bash
cd /root/innate-os && python -m pytest skills/tests/ -v
```
Expected: All tests PASS (should be ~30+ tests across 5 test files)

- [ ] **Step 2: Verify all 13 new skill files exist**

Run:
```bash
ls -1 skills/drive_velocity.py skills/rotate_in_place.py skills/rotate_angle.py \
  skills/arm_move_to_joints.py skills/arm_follow_trajectory.py \
  skills/gripper_open.py skills/gripper_close.py \
  skills/arm_get_pose.py skills/arm_get_load.py \
  skills/arm_torque_on.py skills/arm_torque_off.py skills/arm_reboot_servos.py \
  skills/head_set_angle.py
```
Expected: All 13 files listed, no errors

- [ ] **Step 3: Verify linting passes**

Run:
```bash
cd /root/innate-os && ruff check skills/drive_velocity.py skills/rotate_in_place.py skills/rotate_angle.py skills/arm_move_to_joints.py skills/arm_follow_trajectory.py skills/gripper_open.py skills/gripper_close.py skills/arm_get_pose.py skills/arm_get_load.py skills/arm_torque_on.py skills/arm_torque_off.py skills/arm_reboot_servos.py skills/head_set_angle.py skills/tests/
```
Expected: No errors

- [ ] **Step 4: Final commit if any lint fixes were needed**

```bash
git add -A skills/
git status
# Only commit if there are changes
git diff --cached --quiet || git commit -m "fix: lint fixes for physical skills compendium"
```
