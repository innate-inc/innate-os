from typing import List
from dataclasses import dataclass

from skills.draw_waypoints_utils import datatypes
from skills.draw_waypoints_utils.produce_waypoints import produce_waypoints
from skills import arm_move_to_xyz


@dataclass
class ArmPose:
    x: float
    y: float
    z: float
    roll: float
    pitch: float
    yaw: float


class ArmDrawImageAsWaypoints(arm_move_to_xyz.ArmMoveToXYZ):        
    @property
    def name(self):
        return "arm_draw_image_as_waypoints"

    def guidelines(self):
        return (
            "TODO(rbenefo): Fill this out"
        )
        
    def execute(self, image_path: str):
        """
        Draw the image by converting it to waypoints and moving the arm through them.
        
        Args:
            image_path: Path to the image to draw. Should be a simple black and white line drawing for best results.
        """
        actions = produce_waypoints(image_path)
        ## Produce XYZ waypoints from actions
        poses: List[ArmPose] = []
        ## TODO(rbenefo): Figure out how to get to 0 pose.
        current_pos = ArmPose(x=0.3, y=0.0, z=0.1, roll=0.0, pitch=0.0, yaw=0.0)  # Starting position of the arm
        for action in actions:
            if action == datatypes.ActionType.LIFT:
                LIFT_HEIGHT = 0.3 # m, approx 1 foot
                pose = ArmPose(current_pos.x, current_pos.y, LIFT_HEIGHT, \
                    current_pos.roll, current_pos.pitch, current_pos.yaw)
            elif action == datatypes.ActionType.DROP:
                DRAW_HEIGHT = 0.05 # m, TODO(rbenefo): Adjust based on desired pressure
                pose = ArmPose(current_pos.x, current_pos.y, DRAW_HEIGHT,
                    current_pos.roll, current_pos.pitch, current_pos.yaw)
            elif action == datatypes.ActionType.WAYPOINT:
                ## TODO(rbenefo): Adjust based on camera intrinsics
                WAYPOINT_SCALE = 0.001 # m per pixel
                pose = ArmPose(
                    x=current_pos.x + action.waypoint.x * WAYPOINT_SCALE,
                    y=current_pos.y + action.waypoint.y * WAYPOINT_SCALE,
                    z=current_pos.z,
                    roll=current_pos.roll,
                    pitch=current_pos.pitch,
                    yaw=current_pos.yaw
                )
            poses.append(pose)
            current_pos = pose
        
        for pose in poses:
            result = super().execute(
                x=pose.x, y=pose.y, z=pose.z,
                roll=pose.roll, pitch=pose.pitch, yaw=pose.yaw,
                duration=0.5
            )
            if result[1] != arm_move_to_xyz.SkillResult.SUCCESS:
                return f"Failed to move to waypoint ({pose.x}, {pose.y}, {pose.z})", arm_move_to_xyz.SkillResult.FAILURE
        return "Successfully drew image", arm_move_to_xyz.SkillResult.SUCCESS
