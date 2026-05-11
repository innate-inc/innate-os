from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
import time
import cv2
from typing import Optional
from pydantic import BaseModel

from src.agent.types import DirectiveCmd, BrainActiveCmd

router = APIRouter()


# Create a model for the reset robot request
class ResetRobotRequest(BaseModel):
    memory_state: Optional[str] = None
    position: Optional[list[float]] = None
    orientation: Optional[list[float]] = None


# Create a model for the brain activation request
class SetBrainActiveRequest(BaseModel):
    active: bool


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


@router.get("/stack_metrics")
def stack_metrics(request: Request):
    """Return lightweight simulator/runtime metrics for local stack dashboards."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {
                "ready": False,
                "queue_sizes": {},
                "fps_by_camera": {},
                "latest_frame_age_by_camera": {},
            }
        )

    metrics = shared_queues.get_runtime_metrics()
    return JSONResponse({"ready": True, **metrics})


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


@router.post("/set_brain_active")
async def set_brain_active(request: Request, brain_request: SetBrainActiveRequest):
    """
    Activates or deactivates the brain by sending a command to the agent.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is not None:
        try:
            shared_queues.sim_to_agent.put_nowait(
                BrainActiveCmd(active=brain_request.active)
            )
            return {"status": "brain_command_enqueued"}
        except Exception:
            return {"status": "queue_full"}
    else:
        return {"status": "no_shared_queues"}


@router.get("/get_available_agents")
def get_available_agents(request: Request):
    """
    Returns the list of available agents/directives from the robot brain.
    Each agent includes: id, display_name, display_icon, prompt, skills.
    Also returns the current and startup agent IDs.
    """
    shared_queues = request.app.state.SHARED_QUEUES

    if shared_queues is None:
        return JSONResponse(
            {
                "agents": [],
                "current_agent_id": None,
                "startup_agent_id": None,
                "error": "Simulation not initialized",
            },
            status_code=200,
        )

    agents, current_agent_id, startup_agent_id = shared_queues.get_available_agents()

    # Convert AgentInfo namedtuples to dicts for JSON serialization
    agents_data = [
        {
            "id": agent.id,
            "display_name": agent.display_name,
            "display_icon": agent.display_icon,
            "prompt": agent.prompt,
            "skills": agent.skills,
        }
        for agent in agents
    ]

    return JSONResponse(
        {
            "agents": agents_data,
            "current_agent_id": current_agent_id,
            "startup_agent_id": startup_agent_id,
        }
    )
