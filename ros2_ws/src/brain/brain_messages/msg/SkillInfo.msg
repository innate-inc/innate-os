# brain_messages/msg/SkillInfo.msg
# Information about a single skill (code or physical)
string id                       # Deterministic identifier: "innate-os/<basename>" or "local/<basename>"
string name                     # Cosmetic display name (also used as LLM tool name — must be unique)
string group                    # Folder path inside the skill package ("" = root, "chess", "chess/openings") — for UI grouping only
string type                     # "code", "learned", "replay", "poses"
string guidelines               # Agent guidelines for when to use this skill
string guidelines_when_running  # Agent guidelines while the skill is running
string inputs_json              # JSON-encoded inputs dict, e.g. {"x": "float", "y": "float"}
bool in_training                # True if skill is still in training (missing checkpoint)
int32 episode_count             # Number of recorded episodes (0 for code skills)
string directory                # Absolute path to skill directory (empty for code skills)
bool wheeled                    # Replay skills: True if the base moved during the recording
string module                   # Import path of the class an agent file names for this skill ("innate_skills.wave"); "" when none
string class_name               # That class ("Wave"); "" when none
string load_error               # Non-empty: the file failed to load and this is the error; the skill is not runnable
