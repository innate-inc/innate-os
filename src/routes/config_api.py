from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from typing import Dict, Any, Optional
from pydantic import BaseModel

from src.middleware.auth import get_current_user

# ResetRobotCmd is used by the /reset_robot route
from src.agent.types import ResetRobotCmd

router = APIRouter()


# Pydantic model for the reset request body (copied from video_api)
class ResetRobotRequest(BaseModel):
    memory_state: Optional[str] = None
    position: Optional[list[float]] = None
    orientation: Optional[list[float]] = None


@router.post("/set_environment", dependencies=[Depends(get_current_user)])
async def set_environment(request: Request, env_config: Dict[str, Any]):
    """
    Set the environment configuration based on the provided dictionary.

    This endpoint allows configuring the simulation environment.
    The exact structure of `env_config` is flexible for now.

    Args:
        env_config: A dictionary representing the desired environment configuration.

    Returns:
        JSON response confirming the environment configuration request was received.
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    # TODO: Implement actual environment update logic in the SimulationNode.
    # This logic would parse the `env_config` dictionary and modify the
    # simulation state accordingly (e.g., add/remove assets, change properties).
    # It might need to send a specific command object via shared_queues.agent_to_sim.
    print(f"[ConfigAPI] Received environment configuration: {env_config}")

    # Example: Send the raw config to the simulation node via a queue
    # try:
    #     # Define a command type for environment configuration if needed
    #     # Example: env_update_cmd = EnvironmentUpdateCmd(config=env_config)
    #     # shared_queues.agent_to_sim.put_nowait(env_update_cmd)
    #     pass # Replace with actual command sending
    # except Exception as e:
    #     return JSONResponse(
    #         {"status": "error", "message": f"Failed queue environment update: {e}"},
    #         status_code=500,
    #     )

    return JSONResponse(
        {
            "status": "success",
            "message": "Environment configuration request received. "
            "Processing will occur in simulation node.",
            "received_config": env_config,  # Echoing back the received config
        }
    )


# --- Moved Routes ---


@router.post("/reset_robot", dependencies=[Depends(get_current_user)])
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
            # 503 Service Unavailable is appropriate if the queue is full
            return JSONResponse({"status": "queue_full"}, status_code=503)

        response = {"status": "reset_enqueued", "memory_state": memory_state}

        if pose:
            response["pose"] = {
                "position": list(reset_request.position),  # Ensure list for JSON
                "orientation": list(reset_request.orientation),  # Ensure list for JSON
            }

        return JSONResponse(response)
    else:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )


@router.post("/shutdown", dependencies=[Depends(get_current_user)])
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
    print("[ConfigAPI] Shutdown requested via API endpoint")
    shared_queues.exit_event.set()

    return JSONResponse({"status": "success", "message": "Shutdown initiated"})
