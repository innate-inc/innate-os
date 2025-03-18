from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
import time
import cv2

from src.agent.types import ResetRobotCmd, DirectiveCmd

router = APIRouter()


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


@router.post("/reset_robot")
async def reset_robot(request: Request):
    """
    Enqueues a reset command to move the robot back to its origin.
    Retrieves the shared queues from the application's state.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is not None:
        try:
            shared_queues.agent_to_sim.put_nowait(ResetRobotCmd())
            shared_queues.sim_to_agent.put_nowait(ResetRobotCmd())
        except Exception:
            return {"status": "queue_full"}
        return {"status": "reset_enqueued"}
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
