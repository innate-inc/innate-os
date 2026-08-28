"""Commanded base limits for the virtual MARS.

The simulator accepts the fastest mode exposed by ``mars_app``. Physics has a
separate, looser safety governor in ``world.py`` for collision impulses.
"""

MAX_LINEAR = 0.8  # m/s: motion_control.max_speed (0.4) * Mad scale (2.0)
MAX_YAW = 2.0  # rad/s: motion_control.max_angular_speed (1.0) * Mad scale (2.0)


def clamp_cmd_vel(vx: float, wz: float) -> tuple[float, float]:
    """Clamp a requested twist to the simulator's supported drive envelope."""
    return (
        max(-MAX_LINEAR, min(MAX_LINEAR, vx)),
        max(-MAX_YAW, min(MAX_YAW, wz)),
    )
