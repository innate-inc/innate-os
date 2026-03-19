from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from typing import Dict, Any, Optional
from pydantic import BaseModel, root_validator
import time  # Add time import for timestamp
import os  # Need os for path joining
import json  # Need json for loading file
import asyncio
import uuid

# ResetRobotCmd is used by the /reset_robot route
# SetEnvironmentCmd is used to send the config to the simulation node
# BrainActiveCmd is used to activate/deactivate the brain via rosbridge
from src.agent.types import ResetRobotCmd, SetEnvironmentCmd, BrainActiveCmd
from src.runtime_logging import SIM_LOG_MODES

router = APIRouter()
SET_ENV_APPLY_TIMEOUT_S = 30.0
SET_ENV_APPLY_POLL_INTERVAL_S = 0.02


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


class SetSimLogConfigRequest(BaseModel):
    mode: str


async def wait_for_environment_apply_result(
    shared_queues, request_id: str, timeout_s: float = SET_ENV_APPLY_TIMEOUT_S
):
    """Wait for SimulationNode to report set_environment success/failure."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = shared_queues.pop_environment_apply_result(request_id)
        if result is not None:
            return result
        await asyncio.sleep(SET_ENV_APPLY_POLL_INTERVAL_S)
    return None


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
    request_id = str(uuid.uuid4())

    try:
        set_env_cmd = SetEnvironmentCmd(
            config=env_config, timestamp=time.time(), request_id=request_id
        )
        shared_queues.agent_to_sim.put_nowait(set_env_cmd)
        # Don't log full config here for brevity/security
        print("[ConfigAPI] Enqueued SetEnvironmentCmd")

    except Exception as e:
        # Re-raise as HTTPException for consistent API error handling
        raise HTTPException(
            status_code=500, detail=f"Failed to queue environment update: {e}"
        )

    apply_result = await wait_for_environment_apply_result(shared_queues, request_id)
    if apply_result is None:
        raise HTTPException(
            status_code=504,
            detail="Timed out waiting for environment application result from simulator.",
        )

    if not apply_result.get("success", False):
        raise HTTPException(
            status_code=400,
            detail=apply_result.get("error") or "Simulator failed to apply environment.",
        )

    return JSONResponse(
        {
            "status": "success",
            "message": "Environment configuration applied.",
            "request_id": request_id,
            # Optionally include name if loaded from file
            "source": (
                f"file: {body.config_name}.json"
                if body.config_name
                else "direct config"
            ),
        }
    )


@router.get("/sim_log_config")
def get_sim_log_config(request: Request):
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    return JSONResponse(
        {
            "status": "success",
            "mode": shared_queues.get_sim_log_mode(),
            "available_modes": list(SIM_LOG_MODES),
        }
    )


@router.post("/sim_log_config")
def set_sim_log_config(request: Request, body: SetSimLogConfigRequest):
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    mode = (body.mode or "").strip().lower()
    if mode not in SIM_LOG_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported sim log mode '{body.mode}'. Expected one of: {', '.join(SIM_LOG_MODES)}",
        )

    applied_mode = shared_queues.set_sim_log_mode(mode)
    print(f"[ConfigAPI] Simulator log mode set to {applied_mode}")
    return JSONResponse(
        {
            "status": "success",
            "mode": applied_mode,
            "available_modes": list(SIM_LOG_MODES),
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
    - orientation: [x, y, z, w] quaternion for robot orientation

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


@router.post("/stop_agent")
def stop_agent(request: Request):
    """
    Endpoint to stop the current agent action (e.g., navigation).
    Cancels any ongoing navigation and deactivates the brain via rosbridge.

    Returns:
        JSON response confirming the agent has been stopped
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    # Cancel navigation if active
    if hasattr(shared_queues, "nav_controller") and shared_queues.nav_controller:
        shared_queues.nav_controller.cancel_navigation()

    # Deactivate brain via rosbridge (calls /brain/set_brain_active with data=False)
    try:
        shared_queues.sim_to_agent.put_nowait(BrainActiveCmd(active=False))
        print("[ConfigAPI] Agent stopped via API endpoint (brain deactivated)")
    except Exception as e:
        print(f"[ConfigAPI] Error sending brain deactivate command: {e}")
        return JSONResponse(
            {"status": "error", "message": f"Failed to stop agent: {e}"},
            status_code=500,
        )

    return JSONResponse({"status": "success", "message": "Agent stopped"})


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
