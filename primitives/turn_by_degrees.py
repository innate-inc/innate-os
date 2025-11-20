#!/usr/bin/env python3
import math
import time

from brain_client.primitive_types import Primitive, PrimitiveResult


class TurnByDegrees(Primitive):
    @property
    def name(self):
        return "turn_by_degrees"

    def guidelines(self):
        return (
            "Rotate the robot in place by a given number of degrees. "
            "Positive values rotate counter-clockwise, negative values clockwise. "
            "Parameters: degrees (float), angular_speed_deg_per_sec (optional, default 45)."
        )

    def execute(self, degrees: float, angular_speed_deg_per_sec: float = 45.0):
        if self.mobility is None:
            return "Mobility interface not available", PrimitiveResult.FAILURE

        if angular_speed_deg_per_sec <= 0.0:
            return "angular_speed_deg_per_sec must be > 0", PrimitiveResult.FAILURE

        # Compute rotation direction and duration
        angle_rad = math.radians(abs(degrees))
        angular_speed_rad = math.radians(angular_speed_deg_per_sec)
        duration = angle_rad / angular_speed_rad if angular_speed_rad > 0 else 0.0

        if duration <= 0.0:
            return "Requested rotation too small or zero", PrimitiveResult.FAILURE

        direction = 1.0 if degrees >= 0.0 else -1.0
        commanded_speed = direction * angular_speed_rad

        self._send_feedback(
            f"Turning {'CCW' if direction > 0 else 'CW'} by {degrees:.1f} degrees "
            f"at {angular_speed_deg_per_sec:.1f} deg/s (duration ~ {duration:.1f} s)"
        )

        try:
            self.mobility.rotate_in_place(
                angular_speed=commanded_speed,
                duration=duration,
            )
        except Exception as e:
            self.logger.error(f"TurnByDegrees: exception while commanding rotation: {e}")
            return f"Failed to command rotation: {e}", PrimitiveResult.FAILURE

        # Keep the primitive "running" approximately for the duration of the turn
        start_time = time.time()
        last_feedback_time = start_time
        while True:
            elapsed = time.time() - start_time
            if elapsed >= duration:
                break

            # Optional periodic feedback while turning
            if elapsed - last_feedback_time >= 1.0:
                remaining = max(0.0, duration - elapsed)
                self._send_feedback(
                    f"Still turning; approx {remaining:.1f}s remaining to complete {degrees:.1f} deg turn"
                )
                last_feedback_time = elapsed

            time.sleep(0.1)

        return (
            f"Requested rotation of {degrees:.1f} degrees (approx duration {duration:.1f} s)",
            PrimitiveResult.SUCCESS,
        )

    def cancel(self):
        # For now, cancellation is best-effort via sending a zero-velocity command
        if self.mobility is not None:
            try:
                self.mobility.send_cmd_vel(0.0, 0.0, duration=0.0)
            except Exception:
                pass
        return "TurnByDegrees cancellation requested"
