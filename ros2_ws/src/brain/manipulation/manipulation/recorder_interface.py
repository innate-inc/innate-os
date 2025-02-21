#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import threading
import sys

# Import service types.
from brain_messages.srv import ManipulationTask
from std_srvs.srv import Trigger

# Import status message type.
from brain_messages.msg import RecorderStatus

class TerminalInterface(Node):
    def __init__(self):
        super().__init__('terminal_interface')
        # Create service clients.
        self.new_task_client = self.create_client(ManipulationTask, 'new_task')
        self.new_episode_client = self.create_client(Trigger, 'new_episode')
        self.save_episode_client = self.create_client(Trigger, 'save_episode')
        self.cancel_episode_client = self.create_client(Trigger, 'cancel_episode')
        self.end_task_client = self.create_client(Trigger, 'end_task')

        # Create subscription for recorder status.
        self.create_subscription(
            RecorderStatus, '/recorder_status', self.status_callback, 10
        )

        # Start a thread for reading terminal input.
        thread = threading.Thread(target=self.run_interface, daemon=True)
        thread.start()

    def run_interface(self):
        """Interactive command-line interface loop."""
        self.print_menu()
        while rclpy.ok():
            try:
                command = input("Enter command: ").strip().lower()
            except EOFError:
                break  # End of input
            if command == 'nt':
                # New task: ask for task name, description, and mobile flag.
                task_name = input("Enter task name: ")
                task_description = input("Enter task description: ")
                mobile_flag_str = input("Is this a mobile task? (y/n): ").strip().lower()
                mobile_flag = True if mobile_flag_str == 'y' else False
                self.call_new_task(task_name, task_description, mobile_flag)
            elif command == 'ne':
                self.call_new_episode()
            elif command == 'se':
                self.call_save_episode()
            elif command == 'ce':
                self.call_cancel_episode()
            elif command == 'et':
                self.call_end_task()
            elif command == 'm':
                self.print_menu()
            elif command == 'q':
                self.get_logger().info("Quitting terminal interface.")
                rclpy.shutdown()
                break
            else:
                print("Unknown command. Press 'm' to see the menu.")

    def print_menu(self):
        print("\n===== Recorder Terminal Interface =====")
        print("Commands:")
        print("  nt - New Task")
        print("  ne - New Episode")
        print("  se - Save Episode")
        print("  ce - Cancel Episode")
        print("  et - End Task")
        print("  m  - Show this menu")
        print("  q  - Quit")
        print("=======================================\n")

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
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None:
            print(f"New Task result: success={future.result().success}")
        else:
            print("Failed to call new_task service.")

    def call_new_episode(self):
        request = Trigger.Request()
        if not self.new_episode_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("New episode service not available.")
            return

        future = self.new_episode_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None:
            print(f"New Episode result: success={future.result().success}, message='{future.result().message}'")
        else:
            print("Failed to call new_episode service.")

    def call_save_episode(self):
        request = Trigger.Request()
        if not self.save_episode_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("Save episode service not available.")
            return

        future = self.save_episode_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None:
            print(f"Save Episode result: success={future.result().success}, message='{future.result().message}'")
        else:
            print("Failed to call save_episode service.")

    def call_cancel_episode(self):
        request = Trigger.Request()
        if not self.cancel_episode_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("Cancel episode service not available.")
            return

        future = self.cancel_episode_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None:
            print(f"Cancel Episode result: success={future.result().success}, message='{future.result().message}'")
        else:
            print("Failed to call cancel_episode service.")

    def call_end_task(self):
        request = Trigger.Request()
        if not self.end_task_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error("End task service not available.")
            return

        future = self.end_task_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        if future.result() is not None:
            print(f"End Task result: success={future.result().success}, message='{future.result().message}'")
        else:
            print("Failed to call end_task service.")

    # ---------- Status Callback ----------
    def status_callback(self, msg: RecorderStatus):
        print("\n--- Recorder Status Update ---")
        print(f"Current Task: {msg.current_task_name}")
        print(f"Episode Number: {msg.episode_number}")
        print(f"Status: {msg.status}")
        print("------------------------------\n")
        # Reprint menu prompt to avoid clobbering the terminal.
        print("Enter command (or 'm' for menu): ", end='', flush=True)

def main(args=None):
    rclpy.init(args=args)
    node = TerminalInterface()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Terminal interface interrupted.")
    finally:
        node.destroy_node()
        rclpy.shutdown()
        sys.exit(0)

if __name__ == '__main__':
    main()
