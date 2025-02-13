from aiortc import MediaStreamTrack
from av import VideoFrame
import asyncio


class SimulationVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, shared_queues, camera_name="first_person"):
        super().__init__()
        self.shared_queues = shared_queues
        self.camera_name = camera_name

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = None
        # Wait until a frame is available from the desired camera feed
        while frame is None:
            frame = self.shared_queues.latest_frames.get(self.camera_name)
            if frame is None:
                await asyncio.sleep(0.01)
        # Convert the NumPy frame (assumed BGR) to an av.VideoFrame
        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame
