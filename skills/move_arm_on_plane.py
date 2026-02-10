#!/usr/bin/env python3
"""
Move Arm on Plane Skill - Move arm to XY positions on a fixed plane.
"""

import math
import time

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class MoveArmOnPlane(Skill):
    """Move the arm to XY positions on a fixed horizontal plane."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    # Fixed constants
    FIXED_Z = 0.15
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57  # Pointing downward
    
    # X bounds
    X_MIN = 0.1
    X_MAX = 0.4
    
    # Y bounds
    Y_MIN = -0.2
    Y_MAX = 0.1
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "move_arm_on_plane"
    
    def guidelines(self):
        return (
            f"Move the arm end-effector on a fixed plane. "
            f"Only X and Y can be specified. "
            f"X must be between {self.X_MIN} and {self.X_MAX} meters. "
            f"Y must be between {self.Y_MIN} and {self.Y_MAX} meters. "
            f"Z is fixed at {self.FIXED_Z}m, base pitch is {self.FIXED_PITCH} rad (pointing down). "
            f"An optional pitch_deviation (radians) can be provided to offset the pitch from the default. "
            f"Returns CANCELLED if position is out of bounds."
        )
    
    def execute(
        self,
        x: float,
        y: float,
        pitch_deviation: float = 0.0,
        duration: int = 1
    ):
        """
        Move arm to XY position on a fixed plane.
        
        Args:
            x: Target x position in meters
            y: Target y position in meters
            pitch_deviation: Deviation from the fixed pitch in radians
            duration: Motion duration in seconds
        """
        self._cancelled = False
        
        # Validate X bounds
        if x < self.X_MIN or x > self.X_MAX:
            self.logger.warning(
                f"X position {x} is out of bounds [{self.X_MIN}, {self.X_MAX}]. Cancelling."
            )
            return f"X position {x} out of bounds [{self.X_MIN}, {self.X_MAX}]", SkillResult.CANCELLED
        
        # Validate Y bounds
        if y < self.Y_MIN or y > self.Y_MAX:
            self.logger.warning(
                f"Y position {y} is out of bounds [{self.Y_MIN}, {self.Y_MAX}]. Cancelling."
            )
            return f"Y position {y} out of bounds [{self.Y_MIN}, {self.Y_MAX}]", SkillResult.CANCELLED
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        pitch = self.FIXED_PITCH + pitch_deviation
        
        self.logger.info(
            f"Moving arm on plane to X={x}, Y={y} "
            f"(Z={self.FIXED_Z}, roll={self.FIXED_ROLL}, pitch={pitch}, yaw={self.FIXED_YAW}) "
            f"over {duration}s"
        )
        
        joint_positions = self.manipulation.solve_ik(
            x=x,
            y=y,
            z=self.FIXED_Z,
            roll=self.FIXED_ROLL,
            pitch=pitch,
            yaw=self.FIXED_YAW,
        )
        
        if joint_positions is None:
            return "Failed to solve IK", SkillResult.FAILURE
        
        self.logger.info(
            f"IK target joint positions: {[f'{j:.4f}' for j in joint_positions]}"
        )
        
        # IK returns 5 joints; append current gripper position for the 6th
        if len(joint_positions) == 5:
            current_gripper = 0.0
            arm_state = self.manipulation._arm_state
            if arm_state is not None and len(arm_state.position) >= 6:
                current_gripper = arm_state.position[5]
            joint_positions.append(current_gripper)
        
        success = self.manipulation.move_to_joint_positions(
            joint_positions, duration=duration
        )
        
        if not success:
            return "Failed to send arm command", SkillResult.FAILURE
        
        # Wait for motion to complete (with cancellation check)
        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            time.sleep(0.1)
        
        # Wait for arm to settle before querying FK
        time.sleep(2.0)
        
        # Gather actual state
        self.manipulation.spin_node_to_refresh_topics()
        arm_state = self.manipulation._arm_state
        fk_pose = self.manipulation.get_current_end_effector_pose()
        fk_rpy = self.manipulation.get_current_orientation_rpy()
        
        # ANSI helpers
        BOLD = "\033[1m"
        DIM = "\033[2m"
        RST = "\033[0m"
        CYAN = "\033[36m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        RED = "\033[31m"
        MAGENTA = "\033[35m"
        WHITE = "\033[37m"
        
        def color_err(val, thresh_warn=0.01, thresh_bad=0.03):
            """Color an error value: green if small, yellow if moderate, red if large."""
            a = abs(val)
            if a < thresh_warn:
                return f"{GREEN}{val:+.4f}{RST}"
            elif a < thresh_bad:
                return f"{YELLOW}{val:+.4f}{RST}"
            else:
                return f"{RED}{val:+.4f}{RST}"
        
        def color_pct(val, thresh_warn=2.0, thresh_bad=5.0):
            """Color a percentage error."""
            a = abs(val)
            if a < thresh_warn:
                return f"{GREEN}{val:+.2f}%{RST}"
            elif a < thresh_bad:
                return f"{YELLOW}{val:+.2f}%{RST}"
            else:
                return f"{RED}{val:+.2f}%{RST}"
        
        def rel_err(actual, target):
            if abs(target) < 1e-6:
                return 0.0
            return ((actual - target) / target) * 100.0
        
        def deg(rad):
            return math.degrees(rad)
        
        lines = []
        sep = f"{DIM}{'─' * 72}{RST}"
        
        # ── Joint-level comparison ──
        if arm_state is not None and len(arm_state.position) >= 5:
            actual_joints = list(arm_state.position[:5])
            cmd_joints = joint_positions[:5]
            
            lines.append(sep)
            lines.append(f"{BOLD}{MAGENTA}  JOINT POSITION COMPARISON{RST}")
            lines.append(f"  {'Joint':<8} {'Commanded':>10} {'':>7} {'Actual':>10} {'':>7} {'Error':>12} {'':>7}")
            lines.append(f"  {DIM}{'─' * 68}{RST}")
            for i, (cmd, act) in enumerate(zip(cmd_joints, actual_joints)):
                err = act - cmd
                lines.append(
                    f"  {WHITE}J{i+1:<7}{RST} {CYAN}{cmd:>10.4f}{RST} {DIM}{deg(cmd):>6.1f}°{RST}"
                    f" {CYAN}{act:>10.4f}{RST} {DIM}{deg(act):>6.1f}°{RST}"
                    f" {color_err(err)} {DIM}{deg(err):>+6.1f}°{RST}"
                )
        
        # ── IK Error: FK of commanded joints vs Cartesian target ──
        ik_fk = self.manipulation._ik_solution_fk
        if ik_fk is not None:
            ik_fk_pos = ik_fk.pose.position
            q = ik_fk.pose.orientation
            # quaternion to RPY
            sinr = 2 * (q.w * q.x + q.y * q.z)
            cosr = 1 - 2 * (q.x * q.x + q.y * q.y)
            ik_fk_roll = math.atan2(sinr, cosr)
            sinp = 2 * (q.w * q.y - q.z * q.x)
            ik_fk_pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)
            siny = 2 * (q.w * q.z + q.x * q.y)
            cosy = 1 - 2 * (q.y * q.y + q.z * q.z)
            ik_fk_yaw = math.atan2(siny, cosy)
            
            ik_x, ik_y, ik_z = x, y, self.FIXED_Z
            ik_roll, ik_pitch, ik_yaw = self.FIXED_ROLL, pitch, self.FIXED_YAW
            
            lines.append(sep)
            lines.append(f"{BOLD}{MAGENTA}  IK SOLVER ERROR  (FK of commanded joints vs Cartesian target){RST}")
            lines.append(f"  {'':>8} {'Target':>10} {'FK(cmd)':>10} {'AbsErr':>12} {'RelErr':>10}")
            lines.append(f"  {DIM}{'─' * 56}{RST}")
            
            for label, tgt, act in [("X", ik_x, ik_fk_pos.x), ("Y", ik_y, ik_fk_pos.y), ("Z", ik_z, ik_fk_pos.z)]:
                ae = act - tgt
                re = rel_err(act, tgt)
                lines.append(
                    f"  {WHITE}{label:<8}{RST} {CYAN}{tgt:>10.4f}{RST} {CYAN}{act:>10.4f}{RST} "
                    f"{color_err(ae)}  {color_pct(re)}"
                )
            
            lines.append(f"  {DIM}{'─' * 56}{RST}")
            
            for label, tgt, act in [("Roll", ik_roll, ik_fk_roll), ("Pitch", ik_pitch, ik_fk_pitch), ("Yaw", ik_yaw, ik_fk_yaw)]:
                ae = act - tgt
                re = rel_err(act, tgt)
                lines.append(
                    f"  {WHITE}{label:<8}{RST} {CYAN}{tgt:>10.4f}{RST} {DIM}{deg(tgt):>6.1f}°{RST}"
                    f" {CYAN}{act:>10.4f}{RST} {DIM}{deg(act):>6.1f}°{RST}"
                    f" {color_err(ae, 0.02, 0.05)} {DIM}{deg(ae):>+6.1f}°{RST}  {color_pct(re, 3.0, 8.0)}"
                )
        
        # ── Total Error: FK of actual joints vs Cartesian target ──
        if fk_pose is not None and fk_rpy is not None:
            fk_x = fk_pose["position"]["x"]
            fk_y = fk_pose["position"]["y"]
            fk_z = fk_pose["position"]["z"]
            fk_roll, fk_pitch, fk_yaw = fk_rpy["roll"], fk_rpy["pitch"], fk_rpy["yaw"]
            
            ik_x, ik_y, ik_z = x, y, self.FIXED_Z
            ik_roll, ik_pitch, ik_yaw = self.FIXED_ROLL, pitch, self.FIXED_YAW
            
            lines.append(sep)
            lines.append(f"{BOLD}{MAGENTA}  TOTAL ERROR  (FK of actual joints vs Cartesian target){RST}")
            lines.append(f"  {'':>8} {'Target':>10} {'FK(act)':>10} {'AbsErr':>12} {'RelErr':>10}")
            lines.append(f"  {DIM}{'─' * 56}{RST}")
            
            for label, tgt, act in [("X", ik_x, fk_x), ("Y", ik_y, fk_y), ("Z", ik_z, fk_z)]:
                ae = act - tgt
                re = rel_err(act, tgt)
                lines.append(
                    f"  {WHITE}{label:<8}{RST} {CYAN}{tgt:>10.4f}{RST} {CYAN}{act:>10.4f}{RST} "
                    f"{color_err(ae)}  {color_pct(re)}"
                )
            
            lines.append(f"  {DIM}{'─' * 56}{RST}")
            
            for label, tgt, act in [("Roll", ik_roll, fk_roll), ("Pitch", ik_pitch, fk_pitch), ("Yaw", ik_yaw, fk_yaw)]:
                ae = act - tgt
                re = rel_err(act, tgt)
                lines.append(
                    f"  {WHITE}{label:<8}{RST} {CYAN}{tgt:>10.4f}{RST} {DIM}{deg(tgt):>6.1f}°{RST}"
                    f" {CYAN}{act:>10.4f}{RST} {DIM}{deg(act):>6.1f}°{RST}"
                    f" {color_err(ae, 0.02, 0.05)} {DIM}{deg(ae):>+6.1f}°{RST}  {color_pct(re, 3.0, 8.0)}"
                )
            
            lines.append(sep)
        
        self.logger.info("\n" + "\n".join(lines))
        
        # Build plain result message
        if fk_pose is not None and fk_rpy is not None:
            result_msg = (
                f"Arm moved to X={x}, Y={y}. "
                f"FK pos=({fk_x:.4f}, {fk_y:.4f}, {fk_z:.4f}), "
                f"FK RPY=({fk_roll:.4f}, {fk_pitch:.4f}, {fk_yaw:.4f})"
            )
        else:
            result_msg = f"Arm moved to X={x}, Y={y} (FK unavailable)"
        
        return result_msg, SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
