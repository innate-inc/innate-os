from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from aiortc import RTCPeerConnection, RTCSessionDescription
from src.webrtc.SimulationVideoTrack import SimulationVideoTrack

router = APIRouter()


@router.post("/webrtc_offer")
async def webrtc_offer(request: Request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    shared_queues = request.app.state.SHARED_QUEUES
    pc = RTCPeerConnection()

    # First, set the offer from the remote peer...
    await pc.setRemoteDescription(offer)

    # ...then add the simulation video track.
    simulation_track = SimulationVideoTrack(shared_queues, camera_name="first_person")
    pc.addTrack(simulation_track)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return JSONResponse(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )
