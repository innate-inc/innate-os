#!/usr/bin/env python3
"""
Debug tool to help determine pen tip offset from ee_link.

This tool moves the end effector in small increments along its local x, y, z axes
so you can observe where the pen tip moves and figure out the offset.
"""

import sys
import os
import time
import argparse
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from maurice_msgs.srv import GotoJS
from std_srvs.srv import Trigger

from ament_index_python.packages import get_package_share_directory
from urdf_parser_py.urdf import URDF
import PyKDL as kdl

# Import urdf module directly
import importlib.util
maurice_arm_src = os.path.join(os.path.expanduser('~'), 'innate-os', 'ros2_ws', 'src', 'maurice_bot', 'maurice_arm', 'maurice_arm', 'urdf.py')
if os.path.exists(maurice_arm_src):
    spec = importlib.util.spec_from_file_location("maurice_arm_urdf", maurice_arm_src)
    urdf_module = importlib.util.module_from_spec(spec)
    sys.modules["maurice_arm_urdf"] = urdf_module
    spec.loader.exec_module(urdf_module)
    treeFromUrdfModel = urdf_module.treeFromUrdfModel
else:
    raise ImportError("Could not find maurice_arm.urdf module")


class PenOffsetDebug(Node):
    def __init__(self):
        super().__init__('pen_offset_debug')
        
        # Load URDF and build KDL chain
        pkg_dir = get_package_share_directory('maurice_sim')
        urdf_path = f"{pkg_dir}/urdf/maurice.urdf"
        
        if not os.path.exists(urdf_path):
            raise FileNotFoundError(f"URDF not found: {urdf_path}")
        
        robot_model = URDF.from_xml_file(urdf_path)
        ok, tree = treeFromUrdfModel(robot_model)
        
        if not ok:
            raise RuntimeError("Failed to build KDL tree")
        
        self.chain = tree.getChain('base_link', 'ee_link')
        self.fk_solver = kdl.ChainFkSolverPos_recursive(self.chain)
        
        # Current joint state
        self.current_joint_state = None
        self.joint_sub = self.create_subscription(
            JointState,
            '/mars/arm/state',
            self._joint_state_callback,
            10
        )
        
        # Command publisher
        self.command_pub = self.create_publisher(
            Float64MultiArray,
            '/mars/arm/commands',
            10
        )
        
        # Services
        self.goto_js_client = self.create_client(GotoJS, '/mars/arm/goto_js')
        self.torque_on_client = self.create_client(Trigger, '/mars/arm/torque_on')
        
        self.get_logger().info("Waiting for services...")
        self.goto_js_client.wait_for_service(timeout_sec=5.0)
        self.torque_on_client.wait_for_service(timeout_sec=5.0)
        self.get_logger().info("Services ready!")
    
    def _joint_state_callback(self, msg: JointState):
        """Store latest joint state."""
        self.current_joint_state = msg
    
    def get_current_ee_pose(self):
        """Get current end effector pose using FK."""
        if not self.current_joint_state or not self.current_joint_state.position:
            return None
        
        q = kdl.JntArray(min(len(self.current_joint_state.position), self.chain.getNrOfJoints()))
        for i in range(q.rows()):
            q[i] = self.current_joint_state.position[i]
        
        frame = kdl.Frame()
        result = self.fk_solver.JntToCart(q, frame)
        
        if result >= 0:
            pos = frame.p
            rpy = frame.M.GetRPY()
            return {
                'position': (pos.x(), pos.y(), pos.z()),
                'orientation': (rpy[0], rpy[1], rpy[2]),
                'rotation': frame.M,
                'joint_angles': [self.current_joint_state.position[i] for i in range(q.rows())]
            }
        return None
    
    def move_relative_ee_frame(self, dx: float, dy: float, dz: float):
        """
        Move end effector relative to its current frame.
        
        Args:
            dx: movement in end effector x direction (forward)
            dy: movement in end effector y direction (left)
            dz: movement in end effector z direction (down when pointing down)
        """
        current_pose = self.get_current_ee_pose()
        if current_pose is None:
            self.get_logger().error("No current pose available!")
            return False
        
        # Transform relative movement from end effector frame to base frame
        offset_ee = kdl.Vector(dx, dy, dz)
        offset_base = current_pose['rotation'] * offset_ee
        
        # New target position in base frame
        current_pos = current_pose['position']
        target_x = current_pos[0] + offset_base.x()
        target_y = current_pos[1] + offset_base.y()
        target_z = current_pos[2] + offset_base.z()
        
        self.get_logger().info(
            f"Moving ee_link: ({current_pos[0]:.3f}, {current_pos[1]:.3f}, {current_pos[2]:.3f}) -> "
            f"({target_x:.3f}, {target_y:.3f}, {target_z:.3f})"
        )
        self.get_logger().info(
            f"  Relative movement in ee frame: ({dx:.3f}, {dy:.3f}, {dz:.3f})"
        )
        
        # Use IK to solve for new joint angles
        # For now, keep same orientation
        ik_solver = kdl.ChainIkSolverPos_LMA(self.chain, eps=0.001, maxiter=500)
        
        target_frame = kdl.Frame()
        target_frame.p = kdl.Vector(target_x, target_y, target_z)
        target_rot = current_pose['orientation']
        target_frame.M = kdl.Rotation.RPY(target_rot[0], target_rot[1], target_rot[2])
        
        # Get current joint angles
        current_q = kdl.JntArray(min(len(self.current_joint_state.position), self.chain.getNrOfJoints()))
        for i in range(current_q.rows()):
            current_q[i] = self.current_joint_state.position[i]
        
        q_out = kdl.JntArray(self.chain.getNrOfJoints())
        result = ik_solver.CartToJnt(current_q, target_frame, q_out)
        
        if result < 0 and result != -100 and result != -101:
            self.get_logger().error(f"IK failed with code {result}!")
            return False
        
        joint_angles = [q_out[i] for i in range(q_out.rows())]
        # Pad to 6 joints if needed
        if len(joint_angles) < 6:
            joint_angles.extend([0.0] * (6 - len(joint_angles)))
        
        # Move using goto_js service
        req = GotoJS.Request()
        req.data.data = joint_angles
        req.time = 1.0  # 1 second movement
        
        future = self.goto_js_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=3.0)
        
        if future.done():
            result = future.result()
            if result and result.success:
                self.get_logger().info("Movement completed")
                return True
            else:
                self.get_logger().warn(f"Movement failed: {result.message if result else 'timeout'}")
                return False
        else:
            self.get_logger().warn("Movement timed out")
            return False
    
    def interactive_mode(self, step_size: float = 0.01):
        """Interactive mode to move end effector in small steps."""
        print("\n" + "="*60)
        print("PEN OFFSET DEBUG TOOL")
        print("="*60)
        print("This tool moves the end effector in small steps along its local axes.")
        print("Observe where the PEN TIP moves to figure out the offset.")
        print("\nCommands:")
        print("  x+ / x- : Move forward/backward (end effector x)")
        print("  y+ / y- : Move left/right (end effector y)")
        print("  z+ / z- : Move down/up (end effector z, typically pen direction)")
        print("  p      : Print current pose")
        print("  q      : Quit")
        print(f"\nStep size: {step_size*1000:.1f}mm")
        print("="*60 + "\n")
        
        while rclpy.ok():
            try:
                cmd = input("Command [x+/x-/y+/y-/z+/z-/p/q]: ").strip().lower()
                
                if cmd == 'q':
                    break
                elif cmd == 'p':
                    pose = self.get_current_ee_pose()
                    if pose:
                        pos = pose['position']
                        rpy = pose['orientation']
                        print(f"\nCurrent ee_link pose:")
                        print(f"  Position: ({pos[0]:.3f}, {pos[1]:.3f}, {pos[2]:.3f}) m")
                        print(f"  Orientation (RPY): ({rpy[0]:.3f}, {rpy[1]:.3f}, {rpy[2]:.3f}) rad")
                        print(f"  Orientation (RPY deg): ({rpy[0]*57.3:.1f}, {rpy[1]*57.3:.1f}, {rpy[2]*57.3:.1f}) deg\n")
                    else:
                        print("No pose available\n")
                elif cmd == 'x+':
                    self.move_relative_ee_frame(step_size, 0, 0)
                elif cmd == 'x-':
                    self.move_relative_ee_frame(-step_size, 0, 0)
                elif cmd == 'y+':
                    self.move_relative_ee_frame(0, step_size, 0)
                elif cmd == 'y-':
                    self.move_relative_ee_frame(0, -step_size, 0)
                elif cmd == 'z+':
                    self.move_relative_ee_frame(0, 0, step_size)
                elif cmd == 'z-':
                    self.move_relative_ee_frame(0, 0, -step_size)
                else:
                    print("Unknown command\n")
                
                time.sleep(0.5)  # Small delay between commands
                
            except KeyboardInterrupt:
                break
            except EOFError:
                break
        
        print("\nExiting...")


def main(args=None):
    parser = argparse.ArgumentParser(description='Debug tool to find pen tip offset')
    parser.add_argument('--step-size', type=float, default=0.01,
                       help='Step size in meters (default: 0.01 = 10mm)')
    
    args, ros_args = parser.parse_known_args()
    
    rclpy.init(args=ros_args)
    
    try:
        node = PenOffsetDebug()
        
        # Enable torque
        print("Enabling arm torque...")
        req = Trigger.Request()
        future = node.torque_on_client.call_async(req)
        rclpy.spin_until_future_complete(node, future, timeout_sec=5.0)
        if future.result():
            print("Torque enabled")
        time.sleep(1.0)
        
        # Wait for joint state
        print("Waiting for joint state...")
        while node.current_joint_state is None and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
        
        if node.current_joint_state:
            print("Joint state received!")
            node.interactive_mode(step_size=args.step_size)
        else:
            print("ERROR: No joint state received!")
        
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

