from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from typing import Dict, Any, Optional
from pydantic import BaseModel, root_validator
import time  # Add time import for timestamp
import os  # Need os for path joining
import json  # Need json for loading file

# ResetRobotCmd is used by the /reset_robot route
# SetEnvironmentCmd is used to send the config to the simulation node
from src.agent.types import ResetRobotCmd, SetEnvironmentCmd

router = APIRouter()


# Pydantic model for the reset request body (copied from video_api)
class ResetRobotRequest(BaseModel):
    memory_state: Optional[str] = None
    position: Optional[list[float]] = None
    orientation: Optional[list[float]] = None


# Pydantic model for the set environment request body
class SetEnvironmentRequest(BaseModel):
    config: Optional[Dict[str, Any]] = None
    config_name: Optional[str] = None

    @root_validator(pre=True)
    def check_config_or_name_provided(cls, values):
        config, config_name = values.get("config"), values.get("config_name")
        if config is not None and config_name is not None:
            raise ValueError("Provide either 'config' or 'config_name', not both.")
        if config is None and config_name is None:
            raise ValueError("Either 'config' or 'config_name' must be provided.")
        return values


@router.post("/set_environment")
# Update signature to use the new request model
async def set_environment(request: Request, body: SetEnvironmentRequest):
    """
    Set the environment configuration either by providing a full configuration
    dictionary directly or by specifying the name of a config file to load.

    Args:
        body: Request body containing either `config` (Dict) or `config_name` (str).

    Returns:
        JSON response confirming the environment configuration request was received.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    env_config = None

    # Check if we have valid shared_queues
    if shared_queues is None:
        # Use HTTPException for standard FastAPI error handling
        raise HTTPException(status_code=500, detail="Simulation not initialized")

    # Determine the config dictionary (load from file or use directly)
    if body.config_name:
        config_name = body.config_name
        print(f"[ConfigAPI] Loading environment config file: {config_name}.json")
        try:
            # Construct path relative to project root
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            config_path = os.path.join(
                project_root, "data", "environments", f"{config_name}.json"
            )

            with open(config_path, "r") as f:
                env_config = json.load(f)
            print(f"[ConfigAPI] Successfully loaded config from {config_path}")
        except FileNotFoundError:
            raise HTTPException(
                status_code=400,
                detail=f"Configuration file '{config_name}.json' not found.",
            )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in configuration file '{config_name}.json'.",
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error loading configuration file: {e}"
            )

    elif body.config:
        print("[ConfigAPI] Using direct environment configuration from request body.")
        env_config = body.config

    # This case should be caught by Pydantic validation, but double-check
    if env_config is None:
        raise HTTPException(
            status_code=400,
            detail="Internal error: Could not determine environment configuration.",
        )

    # --- Send the command to the simulation ---
    try:
        set_env_cmd = SetEnvironmentCmd(config=env_config, timestamp=time.time())
        shared_queues.agent_to_sim.put_nowait(set_env_cmd)
        # Don't log full config here for brevity/security
        print("[ConfigAPI] Enqueued SetEnvironmentCmd")

    except Exception as e:
        # Re-raise as HTTPException for consistent API error handling
        raise HTTPException(
            status_code=500, detail=f"Failed to queue environment update: {e}"
        )

    return JSONResponse(
        {
            "status": "success",
            "message": "Environment configuration command sent to simulation.",
            # Optionally include name if loaded from file
            "source": (
                f"file: {body.config_name}.json"
                if body.config_name
                else "direct config"
            ),
        }
    )


# --- Moved Routes ---


@router.post("/reset_robot")
async def reset_robot(
    request: Request, reset_request: Optional[ResetRobotRequest] = None
):
    """
    Enqueues a command to reset the robot to its origin or a specified pose.
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
    print("[ConfigAPI] Shutdown requested via API endpoint")
    shared_queues.exit_event.set()

    return JSONResponse({"status": "success", "message": "Shutdown initiated"})
