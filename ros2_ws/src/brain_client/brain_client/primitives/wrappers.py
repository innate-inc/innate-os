import asyncio
import traceback

import rclpy
from typing import Dict, Any
from brain_client.primitives.types import Primitive
from brain_client.primitives.navigate_to_position import NavigateToPosition


async def wrap_execution(primitive: Primitive, inputs: Dict[str, Any], logger):
    """
    Wraps an awaitable (the primitive's execution coroutine) and yields status messages.

    :param coro: The awaitable (coroutine) representing the primitive execution.
    :yield: Status update dictionaries.
    """
    # Yield the "started" event
    yield {"status": "started", "message": "Execution started."}
    try:
        # Instantiate the primitive
        primitive = primitive(logger)
        primitive_name = primitive.name
        result_msg, result_success = await primitive.execute(**inputs)
    except asyncio.CancelledError:
        # Yield "interrupted" event if there is a cancellation
        yield {
            "primitive_name": primitive_name,
            "status": "interrupted",
            "message": "Execution was interrupted.",
        }
        raise
    except Exception as e:
        # Yield "failed" event if any exception is raised
        yield {
            "primitive_name": primitive_name,
            "status": "failed",
            "message": f"Execution failed with error: {e}. Traceback: {traceback.format_exc()}",
        }
        raise
    else:
        # If everything goes fine, yield "completed" event
        yield {
            "primitive_name": primitive_name,
            "status": "completed" if result_success else "failed",
            "result_msg": result_msg,
        }


def run_primitive(primitive: Primitive, inputs: Dict[str, Any]):
    asyncio.run(wrap_execution(primitive, inputs))


async def run_primitive_in_node(primitive: Primitive, inputs: Dict[str, Any]):
    rclpy.init()
    node = rclpy.create_node("primitive_node")
    primitive = primitive(node.get_logger())
    gen = wrap_execution(primitive, inputs)
    async for status in gen:
        print(status)
    rclpy.shutdown()


if __name__ == "__main__":
    run_primitive_in_node(NavigateToPosition, {"x": 0.0, "y": 0.0})
