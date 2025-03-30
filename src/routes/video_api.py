from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
import time
import cv2
from typing import Optional
from pydantic import BaseModel

from src.agent.types import ResetRobotCmd, DirectiveCmd

router = APIRouter()


# Create a model for the reset robot request
class ResetRobotRequest(BaseModel):
    memory_state: Optional[str] = None
    position: Optional[list[float]] = None
    orientation: Optional[list[float]] = None


def mjpeg_generator(shared_queues, camera_name="first_person"):
    """
    Continuously yields JPEG frames from the simulation.
    Uses the shared_queues (attached on app.state) for the latest frames.
    """
    while True:
        if shared_queues is None:
            time.sleep(0.1)
            continue

        frame = shared_queues.latest_frames.get(camera_name)
        if frame is None:
            time.sleep(0.01)
            continue

        shared_queues.latest_frames[camera_name] = None

        success, encoded_image = cv2.imencode(".jpg", frame)
        if not success:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + encoded_image.tobytes() + b"\r\n"
        )


@router.get("/video_feeds_ready")
def video_feeds_ready(request: Request):
    """
    Simple endpoint to check if the video feeds are ready.
    Just checks if shared_queues exists, which indicates the simulation is running.
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Simply check if shared_queues exists
    is_ready = shared_queues is not None

    return JSONResponse(
        {
            "ready": is_ready,
            "message": (
                "Simulation is running" if is_ready else "Simulation not initialized"
            ),
        }
    )


@router.get("/video_feed", include_in_schema=False)
def video_feed(request: Request):
    """
    Streaming endpoint which returns the primary camera feed.
    Retrieves the shared queues from the application's state.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    return StreamingResponse(
        mjpeg_generator(shared_queues, "first_person"),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/video_feed_chase", include_in_schema=False)
def video_feed_chase(request: Request):
    """
    Streaming endpoint which returns the chase camera feed.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    return StreamingResponse(
        mjpeg_generator(shared_queues, "chase"),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/get_robot_position")
def get_robot_position(request: Request):
    """
    Returns the current 3D position (x, y, z) of the robot.
    Uses the SharedQueues' direct robot position tracking.

    Returns:
        JSON response with position [x, y, z] and timestamp
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {
                "position": [0.0, 0.0, 0.0],  # Default position
                "timestamp": time.time(),
                "error": "Simulation not initialized",
            },
            status_code=200,  # Still return 200 to avoid breaking clients
        )

    # Retrieve position and timestamp directly from shared queues
    position, timestamp = shared_queues.get_robot_position()

    # Convert any NumPy types to native Python types
    position = [float(p) for p in position]  # Convert to native Python floats

    return JSONResponse(
        {
            "position": position,
            "timestamp": float(timestamp),  # Ensure timestamp is also a Python float
        }
    )


@router.post("/reset_robot")
async def reset_robot(
    request: Request, reset_request: Optional[ResetRobotRequest] = None
):
    """
    Enqueues a reset command to move the robot back to its origin or to a specified pose.
    Optionally specifies a memory state to load.
    Retrieves the shared queues from the application's state.

    Request body can include:
    - memory_state: string identifier for memory state to load
    - position: [x, y, z] coordinates for robot position
    - orientation: [w, x, y, z] quaternion for robot orientation

    If position and orientation are both provided, they will be used as the new pose.
    Otherwise, the default pose will be used.
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Get memory_state from request body if provided
    memory_state = None
    pose = None

    if reset_request is not None:
        memory_state = reset_request.memory_state

        # If both position and orientation are provided, combine them into pose
        if reset_request.position is not None and reset_request.orientation is not None:
            position = tuple(reset_request.position)
            orientation = tuple(reset_request.orientation)
            pose = (position, orientation)

    if shared_queues is not None:
        try:
            reset_cmd = ResetRobotCmd(memory_state=memory_state, pose=pose)
            shared_queues.agent_to_sim.put_nowait(reset_cmd)
            shared_queues.sim_to_agent.put_nowait(reset_cmd)
        except Exception:
            return {"status": "queue_full"}

        response = {"status": "reset_enqueued", "memory_state": memory_state}

        if pose:
            response["pose"] = {
                "position": reset_request.position,
                "orientation": reset_request.orientation,
            }

        return response
    else:
        return {"status": "no_shared_queues"}


@router.post("/set_directive")
async def set_directive(request: Request, directive: dict):
    """
    Enqueues a directive command to update the robot's behavior.
    Retrieves the shared queues from the application's state.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is not None:
        try:
            shared_queues.sim_to_agent.put_nowait(
                DirectiveCmd(directive=directive["text"])
            )
        except Exception:
            return {"status": "queue_full"}
        return {"status": "directive_enqueued"}
    else:
        return {"status": "no_shared_queues"}


@router.post("/shutdown")
def shutdown_simulator(request: Request):
    """
    Endpoint to gracefully shut down the simulator.
    Sets the exit event in shared queues to signal all threads to stop.

    Returns:
        JSON response confirming shutdown has been initiated
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    # Set the exit event to signal all threads to stop
    print("[API] Shutdown requested via API endpoint")
    shared_queues.exit_event.set()

    return JSONResponse({"status": "success", "message": "Shutdown initiated"})
