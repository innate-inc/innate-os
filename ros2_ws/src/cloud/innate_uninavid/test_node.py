"""Quick smoke tests for _compute_cmd_vel."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..") )

from innate_uninavid.node import (
    _compute_cmd_vel, IMAGE_SEND_HZ,
    ACTION_STOP, ACTION_FORWARD, ACTION_LEFT, ACTION_RIGHT,
)

# (ws_message, expected_linear_x, expected_angular_z)
checks = [
    (str(ACTION_STOP),    0.0,  0.0),
    (str(ACTION_FORWARD), 0.3,  0.0),
    (str(ACTION_LEFT),    0.0,  0.8),
    (str(ACTION_RIGHT),   0.0, -0.8),
    (b"1",                0.3,  0.0),   # bytes input
    ("2\n",               0.0,  0.8),   # trailing whitespace
]

names = {0: "STOP", 1: "FORWARD", 2: "LEFT", 3: "RIGHT"}

for msg, exp_lx, exp_az in checks:
    t = _compute_cmd_vel(msg)
    raw_str = msg.decode() if isinstance(msg, bytes) else msg
    try:
        label = names.get(int(raw_str.strip()), raw_str.strip())
    except (ValueError, AttributeError):
        label = repr(msg)
    assert t is not None, f"{msg!r} returned None"
    assert abs(t.linear.x - exp_lx) < 1e-9, f"{label}: linear.x {t.linear.x} != {exp_lx}"
    assert abs(t.angular.z - exp_az) < 1e-9, f"{label}: angular.z {t.angular.z} != {exp_az}"
    print(f"  {label:<10}  linear.x={t.linear.x:+.1f}  angular.z={t.angular.z:+.1f}  OK")

assert _compute_cmd_vel("99") is None
assert _compute_cmd_vel("nonsense") is None
assert _compute_cmd_vel("") is None
print("  unknown/empty  -> None  OK")
assert IMAGE_SEND_HZ == 1.0
print(f"  IMAGE_SEND_HZ = {IMAGE_SEND_HZ}  OK")
print("\nAll checks passed.")
