"""
Utility helpers for stereo calibration: head/arm control and file I/O.
"""

import json
import shutil
import threading
import time

import cv2
import rclpy
from std_msgs.msg import Int32, String
from std_srvs.srv import Trigger


# ---------------------------------------------------------------------------
# Head & arm control
# ---------------------------------------------------------------------------

def setup_head_and_arm(node):
    """Set head to +20 degrees and disable arm torque for calibration.

    Stores original state on *node* so that :func:`restore_head_and_arm` can
    undo the changes.

    Args:
        node: StereoCalibrator node instance.
    """
    node.original_head_position = None
    node.original_torque_enabled = None

    # --- Read and set head position ---
    try:
        got_msg = threading.Event()
        head_msg = {'data': None}

        def head_cb(msg):
            head_msg['data'] = msg
            got_msg.set()

        head_sub = node.create_subscription(
            String, '/mars/head/current_position', head_cb, 1)

        # Wait for the executor to deliver the callback (no spin_once needed).
        got_msg.wait(timeout=2.0)
        node.destroy_subscription(head_sub)

        if head_msg['data'] is not None:
            head_data = json.loads(head_msg['data'].data)
            node.original_head_position = int(round(head_data['current_position']))
            node.get_logger().info(f'Current head position: {node.original_head_position} degrees')
        else:
            node.get_logger().warn('Could not read head position (timeout)')

        # Set head to +20 degrees for calibration
        head_pub = node.create_publisher(Int32, '/mars/head/set_position', 1)
        time.sleep(0.1)  # allow publisher discovery
        msg = Int32()
        msg.data = 20
        head_pub.publish(msg)
        node.get_logger().info('Set head position to +20 degrees for calibration')
        node.destroy_publisher(head_pub)

    except Exception as e:
        node.get_logger().warn(f'Failed to set head position: {e}')

    # # --- Read and disable arm torque ---
    # try:
    #     from maurice_msgs.msg import ArmStatus

    #     received_status = {'msg': None}

    #     def status_cb(msg):
    #         received_status['msg'] = msg

    #     status_sub = node.create_subscription(
    #         ArmStatus, '/mars/arm/status', status_cb, 1)

    #     start = time.time()
    #     while received_status['msg'] is None and (time.time() - start) < 2.0:
    #         rclpy.spin_once(node, timeout_sec=0.1)
    #     node.destroy_subscription(status_sub)

    #     if received_status['msg'] is not None:
    #         node.original_torque_enabled = received_status['msg'].is_torque_enabled
    #         node.get_logger().info(
    #             f'Current arm torque: {"ON" if node.original_torque_enabled else "OFF"}')
    #     else:
    #         node.get_logger().warn('Could not read arm status (timeout)')

    #     # Disable arm torque so it's limp during calibration
    #     torque_off_client = node.create_client(Trigger, '/mars/arm/torque_off')
    #     if torque_off_client.wait_for_service(timeout_sec=2.0):
    #         future = torque_off_client.call_async(Trigger.Request())
    #         rclpy.spin_until_future_complete(node, future, timeout_sec=2.0)
    #         if future.result() is not None and future.result().success:
    #             node.get_logger().info('Disabled arm torque for calibration')
    #         else:
    #             node.get_logger().warn('Failed to disable arm torque')
    #     else:
    #         node.get_logger().warn('Arm torque_off service not available')
    #     node.destroy_client(torque_off_client)

    # except Exception as e:
    #     node.get_logger().warn(f'Failed to disable arm torque: {e}')


def restore_head_and_arm(node):
    """Restore head position and arm torque to their original states.

    Args:
        node: StereoCalibrator node instance.
    """
    # --- Restore head position ---
    if node.original_head_position is not None:
        try:
            head_pub = node.create_publisher(Int32, '/mars/head/set_position', 1)
            time.sleep(0.1)  # allow publisher discovery
            msg = Int32()
            msg.data = node.original_head_position
            head_pub.publish(msg)
            node.get_logger().info(
                f'Restored head position to {node.original_head_position} degrees')
            node.destroy_publisher(head_pub)
        except Exception as e:
            node.get_logger().warn(f'Failed to restore head position: {e}')

    # # --- Restore arm torque ---
    # if node.original_torque_enabled is not None and node.original_torque_enabled:
    #     try:
    #         torque_client = node.create_client(Trigger, '/mars/arm/torque_on')
    #         if torque_client.wait_for_service(timeout_sec=2.0):
    #             future = torque_client.call_async(Trigger.Request())
    #             rclpy.spin_until_future_complete(node, future, timeout_sec=2.0)
    #             if future.result() is not None and future.result().success:
    #                 node.get_logger().info('Restored arm torque to ON')
    #             else:
    #                 node.get_logger().warn('Failed to restore arm torque')
    #         else:
    #             node.get_logger().warn('Arm torque_on service not available')
    #         node.destroy_client(torque_client)
    #     except Exception as e:
    #         node.get_logger().warn(f'Failed to restore arm torque: {e}')


# ---------------------------------------------------------------------------
# Calibration file I/O
# ---------------------------------------------------------------------------

def save_calibration(node):
    """Save calibration to YAML file using OpenCV FileStorage.

    Args:
        node: StereoCalibrator node instance (needs ``calibration_data``,
              ``data_directory``).
    """
    try:
        calib_dir = find_calibration_dir(node)
        if calib_dir is None:
            calib_dir = node.data_directory / 'calibration_config'
            calib_dir.mkdir(parents=True, exist_ok=True)
            node.get_logger().info(f'Created new calibration directory: {calib_dir}')

        output_path = calib_dir / 'stereo_calib.yaml'
        backup_path = calib_dir / 'stereo_calib.yaml.backup'

        if output_path.exists():
            shutil.copy(output_path, backup_path)
            node.get_logger().info(f'Backed up existing calibration to: {backup_path}')

        fs = cv2.FileStorage(str(output_path), cv2.FileStorage_WRITE)
        if not fs.isOpened():
            raise RuntimeError(f'Failed to open file for writing: {output_path}')

        fs.write('version', 2)
        fs.write('model', 'pinhole')
        fs.write('image_width', node.calibration_data['image_width'])
        fs.write('image_height', node.calibration_data['image_height'])
        fs.write('K1', node.calibration_data['K1'])
        fs.write('D1', node.calibration_data['D1'])
        fs.write('K2', node.calibration_data['K2'])
        fs.write('D2', node.calibration_data['D2'])
        fs.write('R', node.calibration_data['R'])
        fs.write('T', node.calibration_data['T'])
        fs.write('R1', node.calibration_data['R1'])
        fs.write('R2', node.calibration_data['R2'])
        fs.write('P1', node.calibration_data['P1'])
        fs.write('P2', node.calibration_data['P2'])
        fs.write('Q', node.calibration_data['Q'])

        fs.release()

        node.get_logger().info(f'Calibration saved to: {output_path}')
        import sys
        sys.stdout.flush()
        return str(output_path)
    except Exception as e:
        node.get_logger().error(f'Error saving calibration: {e}')
        raise


def find_calibration_dir(node):
    """Find existing calibration config directory under ``node.data_directory``.

    Returns:
        Path to the first directory whose name contains ``calibration_config``,
        or ``None`` if none is found.
    """
    if not node.data_directory.exists():
        return None

    for entry in node.data_directory.iterdir():
        if entry.is_dir() and 'calibration_config' in entry.name:
            return entry

    return None


def prompt_save(node):
    """Ask user whether to save or discard the calibration, then shut down.

    Args:
        node: StereoCalibrator node instance.
    """
    print('')
    print('Do you want to save this calibration and replace the existing one?')

    try:
        response = input('Type "y" to save, "n" to discard: ').strip().lower()
        if response == 'y':
            try:
                save_calibration(node)
            except Exception as e:
                node.get_logger().error(f'Failed to save calibration: {e}')
        else:
            node.get_logger().info('Calibration discarded.')
    except EOFError:
        node.get_logger().info('Calibration discarded (no input).')
    except Exception as e:
        node.get_logger().error(f'Error during save prompt: {e}')

    # Restore head and arm to original state
    restore_head_and_arm(node)

    node.get_logger().info('Calibration complete. Shutting down...')

    # Shut down rclpy so that rclpy.spin() on the main thread unblocks.
    # This is safe to call from any thread — spin() will raise and main() handles cleanup.
    try:
        rclpy.shutdown()
    except Exception:
        pass
