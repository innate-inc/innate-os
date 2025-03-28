#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import curses

# Import service types.
from brain_messages.srv import ManipulationTask
from std_srvs.srv import Trigger

# Import status message type.
from brain_messages.msg import RecorderStatus


class TerminalInterface(Node):
    def __init__(self):
        super().__init__('terminal_interface')
        # Create service clients.
        self.new_task_client = self.create_client(ManipulationTask, 'recorder/new_task')
        self.new_episode_client = self.create_client(Trigger, 'recorder/new_episode')
        self.save_episode_client = self.create_client(Trigger, 'recorder/save_episode')
        self.cancel_episode_client = self.create_client(Trigger, 'recorder/cancel_episode')
        self.end_task_client = self.create_client(Trigger, 'recorder/end_task')

        # Create subscription for recorder status.
        self.create_subscription(
            RecorderStatus, '/recorder/status', self.status_callback, 10
        )

        # Variable to store latest status info.
        self.latest_status = {}

    def status_callback(self, msg: RecorderStatus):
        # Store latest status for UI display.
        self.latest_status = {
            'current_task_name': msg.current_task_name,
            'episode_number': msg.episode_number,
            'status': msg.status,
        }

    # ---------- Service Call Methods ----------
    def call_new_task(self, task_name, task_description, mobile_flag):
        request = ManipulationTask.Request()
        request.task_name = task_name
        request.task_description = task_description
        request.mobile_task = mobile_flag

        if not self.new_task_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("New task service not available.")
            return

        future = self.new_task_client.call_async(request)
        future.add_done_callback(self.handle_new_task_response)

    def handle_new_task_response(self, future):
        try:
            response = future.result()
            self.get_logger().info(f"New Task result: success={response.success}")
        except Exception as e:
            self.get_logger().error(f"New Task service call failed: {e}")

    def call_new_episode(self):
        request = Trigger.Request()
        if not self.new_episode_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("New episode service not available.")
            return

        future = self.new_episode_client.call_async(request)
        future.add_done_callback(self.handle_new_episode_response)

    def handle_new_episode_response(self, future):
        try:
            response = future.result()
            self.get_logger().info(f"New Episode result: success={response.success}, message='{response.message}'")
        except Exception as e:
            self.get_logger().error(f"New Episode service call failed: {e}")

    def call_save_episode(self):
        request = Trigger.Request()
        if not self.save_episode_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("Save episode service not available.")
            return

        future = self.save_episode_client.call_async(request)
        future.add_done_callback(self.handle_save_episode_response)

    def handle_save_episode_response(self, future):
        try:
            response = future.result()
            self.get_logger().info(f"Save Episode result: success={response.success}, message='{response.message}'")
        except Exception as e:
            self.get_logger().error(f"Save Episode service call failed: {e}")

    def call_cancel_episode(self):
        request = Trigger.Request()
        if not self.cancel_episode_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("Cancel episode service not available.")
            return

        future = self.cancel_episode_client.call_async(request)
        future.add_done_callback(self.handle_cancel_episode_response)

    def handle_cancel_episode_response(self, future):
        try:
            response = future.result()
            self.get_logger().info(f"Cancel Episode result: success={response.success}, message='{response.message}'")
        except Exception as e:
            self.get_logger().error(f"Cancel Episode service call failed: {e}")

    def call_end_task(self):
        request = Trigger.Request()
        if not self.end_task_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("End task service not available.")
            return

        future = self.end_task_client.call_async(request)
        future.add_done_callback(self.handle_end_task_response)

    def handle_end_task_response(self, future):
        try:
            response = future.result()
            self.get_logger().info(f"End Task result: success={response.success}, message='{response.message}'")
        except Exception as e:
            self.get_logger().error(f"End Task service call failed: {e}")


def main_curses(stdscr):
    # Initialize curses settings
    curses.curs_set(0)            # Hide the cursor.
    stdscr.nodelay(True)          # Do not block on getch().
    stdscr.timeout(100)           # Refresh every 100ms.

    rclpy.init(args=None)
    node = TerminalInterface()

    while True:
        stdscr.clear()
        # Draw the menu (top of screen)
        menu_str = ("Commands: n(New Task), e(New Episode), s(Save Episode), "
                    "c(Cancel Episode), t(End Task), m(Menu), q(Quit)")
        stdscr.addstr(0, 0, menu_str)

        # Draw status information (middle of screen)
        stdscr.addstr(2, 0, "Recorder Status:")
        if node.latest_status:
            stdscr.addstr(3, 0, f"Current Task: {node.latest_status.get('current_task_name', 'N/A')}")
            stdscr.addstr(4, 0, f"Episode Number: {node.latest_status.get('episode_number', 'N/A')}")
            stdscr.addstr(5, 0, f"Status: {node.latest_status.get('status', 'N/A')}")
        else:
            stdscr.addstr(3, 0, "No status received yet.")

        stdscr.addstr(7, 0, "Press a command key...")
        stdscr.refresh()

        # Process any ROS events (callbacks, service responses, etc.)
        rclpy.spin_once(node, timeout_sec=0.1)

        # Check for keyboard input
        try:
            key = stdscr.getch()
        except Exception:
            key = -1

        if key == -1:
            continue

        # Map single-key commands to actions.
        if key in [ord('q'), ord('Q')]:
            break
        elif key in [ord('n'), ord('N')]:
            # Temporarily disable non-blocking mode to get full string input.
            stdscr.nodelay(False)
            stdscr.addstr(9, 0, "Enter task name: ")
            stdscr.refresh()
            task_name = stdscr.getstr().decode('utf-8')

            stdscr.addstr(10, 0, "Enter task description: ")
            stdscr.refresh()
            task_description = stdscr.getstr().decode('utf-8')

            stdscr.addstr(11, 0, "Is this a mobile task? (y/n): ")
            stdscr.refresh()
            mobile_flag_str = stdscr.getstr().decode('utf-8').strip().lower()
            mobile_flag = (mobile_flag_str == 'y')

            node.call_new_task(task_name, task_description, mobile_flag)
            stdscr.addstr(13, 0, "New Task requested. Press any key to continue.")
            stdscr.refresh()
            stdscr.getch()
            stdscr.nodelay(True)
        elif key in [ord('e'), ord('E')]:
            node.call_new_episode()
        elif key in [ord('s'), ord('S')]:
            node.call_save_episode()
        elif key in [ord('c'), ord('C')]:
            node.call_cancel_episode()
        elif key in [ord('t'), ord('T')]:
            node.call_end_task()
        elif key in [ord('m'), ord('M')]:
            # Optionally, re-display the menu (the menu is always visible here).
            pass

    # Clean up when exiting the loop.
    node.destroy_node()
    rclpy.shutdown()


def main():
    # Use curses.wrapper to manage setup and teardown of curses.
    curses.wrapper(main_curses)


if __name__ == '__main__':
    main()
