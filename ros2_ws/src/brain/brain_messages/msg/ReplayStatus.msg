# Replay playback status message
string state              # "idle", "ready", "playing", "paused", "finished"
int32 current_frame       # Current frame index (0-indexed)
int32 total_frames        # Total number of frames
float32 current_time_sec  # Current playback time in seconds
float32 total_time_sec    # Total duration in seconds
string episode_id         # e.g., "episode_3"
string task_name          # Name of the task being replayed
