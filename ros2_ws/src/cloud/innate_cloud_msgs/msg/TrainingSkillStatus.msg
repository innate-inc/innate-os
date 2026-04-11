# Snapshot of a single skill: its upload progress and runs.

string training_skill_id
string skill_name
string skill_dir            # absolute path on robot, empty if unknown

# Skill-level upload progress (e.g. uploading training data).
bool has_active_transfer
bool transfer_done           # true once an upload finished (persists for node lifetime)
innate_cloud_msgs/TransferProgress active_transfer

int32 uploaded_episode_count  # episode count at last successful upload (-1 = never uploaded)

innate_cloud_msgs/TrainingRunStatus[] runs
