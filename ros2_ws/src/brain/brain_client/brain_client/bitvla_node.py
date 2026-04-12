#!/usr/bin/env python3
import cv2
import numpy as np
import rclpy
import torch
from geometry_msgs.msg import Twist
from maurice_msgs.srv import GotoJS
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import Float64MultiArray, String
from transformers import AutoModelForVision2Seq, AutoProcessor


IMAGE_QOS = QoSProfile(
    reliability=QoSReliabilityPolicy.BEST_EFFORT,
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=1,
)


class BitVLANode(Node):
    def __init__(self):
        super().__init__("bitvla_node")

        self.declare_parameter("model_path", "/root/models/bitvla")
        self.declare_parameter("instruction", "")
        self.declare_parameter("control_hz", 25)

        model_path = self.get_parameter("model_path").value
        self._instruction = self.get_parameter("instruction").value
        control_hz = self.get_parameter("control_hz").value

        self._head_img = None
        self._wrist_img = None
        self._joint_pos = None

        # Load model
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._processor = AutoProcessor.from_pretrained(model_path)
        self._model = AutoModelForVision2Seq.from_pretrained(
            model_path, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True
        ).to(device).eval()
        self._device = device

        # Subscribers
        self.create_subscription(CompressedImage, "/mars/main_camera/left/image_raw/compressed", self._head_cb, IMAGE_QOS)
        self.create_subscription(CompressedImage, "/mars/arm/image_raw/compressed", self._wrist_cb, IMAGE_QOS)
        self.create_subscription(JointState, "/mars/arm/state", self._joint_cb, 10)
        self.create_subscription(String, "/bitvla/instruction", self._instruction_cb, 10)

        # Publishers / services
        self._cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._goto_js = self.create_client(GotoJS, "/mars/arm/goto_js_v2")

        self.create_timer(1.0 / control_hz, self._step)

    def _decode(self, msg: CompressedImage) -> np.ndarray:
        buf = np.frombuffer(msg.data, dtype=np.uint8)
        return cv2.cvtColor(cv2.imdecode(buf, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)

    def _head_cb(self, msg):   self._head_img = self._decode(msg)
    def _wrist_cb(self, msg):  self._wrist_img = self._decode(msg)
    def _joint_cb(self, msg):  self._joint_pos = list(msg.position[:6])

    def _instruction_cb(self, msg: String):
        self._instruction = msg.data.strip()

    def _step(self):
        if not self._instruction or self._head_img is None or self._wrist_img is None or self._joint_pos is None:
            return

        inputs = self._processor(
            text=self._instruction,
            images=[self._head_img, self._wrist_img],
            return_tensors="pt",
        ).to(self._device)

        with torch.no_grad():
            actions = self._model.predict_action(
                **inputs,
                proprio=torch.tensor(self._joint_pos, dtype=torch.bfloat16).to(self._device),
            )

        action = actions.cpu().numpy()[0]

        req = GotoJS.Request()
        req.data = Float64MultiArray(data=action[:6].tolist())
        req.time = 0.04  # 25 Hz
        self._goto_js.call_async(req)

        twist = Twist()
        twist.linear.x = float(action[6])
        twist.angular.z = float(action[7])
        self._cmd_vel_pub.publish(twist)


def main():
    rclpy.init()
    node = BitVLANode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
