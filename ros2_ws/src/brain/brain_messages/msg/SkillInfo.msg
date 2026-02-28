# brain_messages/msg/SkillInfo.msg
# Information about a single skill (code or physical)
string name                     # Display name / identifier
string type                     # "code", "learned", "replay", "poses"
string guidelines               # Agent guidelines for when to use this skill
string guidelines_when_running  # Agent guidelines while the skill is running
string inputs_json              # JSON-encoded inputs dict, e.g. {"x": "float", "y": "float"}
bool in_training                # True if skill is still in training (missing checkpoint)
int32 episode_count             # Number of recorded episodes (0 for code skills)
string directory                # Absolute path to skill directory (empty for code skills)
