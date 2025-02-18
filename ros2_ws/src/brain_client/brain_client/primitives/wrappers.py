import asyncio
import traceback
import rclpy
from rclpy.node import Node
from std_msgs.msg import String


def run_primitive_in_node(primitive_cls, execute_kwargs, logger):
    # Only initialize if rclpy hasn't been initted already.
    created_context = False
    if not rclpy.ok():
        rclpy.init()
        created_context = True

    node = PrimitiveWrapperNode(primitive_cls)
    try:
        node.execute_primitive(**execute_kwargs)
    except Exception as e:
        logger.error(
            f"Error running primitive: {e}. Traceback: {traceback.format_exc()}"
        )
    finally:
        logger.info("Primitive execution completed.")
        node.destroy_node()
        if created_context:
            rclpy.shutdown()


class PrimitiveWrapperNode(Node):
    def __init__(self, primitive_cls):
        super().__init__("primitive_wrapper_node")
        self.primitive = primitive_cls(self.get_logger())
        self.get_logger().info(
            f"Wrapped primitive '{self.primitive.name}' initialized."
        )

        # Optionally set up additional communication publishers/subscribers
        self.status_pub = self.create_publisher(String, "primitive_status", 10)

    def execute_primitive(self, **kwargs):
        self.primitive.execute(**kwargs)
        self.get_logger().info(f"Primitive '{self.primitive.name}' execution over.")
        self.set_done()

    def set_done(self):
        self.done = True
