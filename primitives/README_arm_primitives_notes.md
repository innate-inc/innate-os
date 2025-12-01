# Arm Primitive Execution Notes

This file captures a few *practical* rules we learned while debugging `move_arm_to_pose` and the arm manipulation stack.

## 1. Avoid blocking the executor inside action callbacks

When the `ExecutePrimitive` action callback runs in `PrimitiveExecutionActionServer`, **do not**:

- Call `rclpy.spin()` or `rclpy.spin_once()` on the same node.
- Call `rclpy.spin_until_future_complete()` on service/action futures.
- Call `wait_for_service()` with a long timeout.
- Sleep for long periods while also holding up all other callbacks.

These patterns can starve the executor and cause symptoms like:

- First primitive works, second primitive goal is never accepted/processed.
- `ros2 action send_goal` hangs after printing `Sending goal:`.

## 2. IK (`solve_ik`) should not spin the node

`ManipulationInterface.solve_ik` used to do this in a loop:

```python
rclpy.spin_once(self.node, timeout_sec=0.01)
```

This was called **from inside** the action server’s execute callback.

Instead, we now:

- Publish the IK target (`ik_delta`).
- Poll for `_ik_solution` with a short `time.sleep(0.01)`.
- Let the main executor thread deliver subscription callbacks.

This keeps IK polling local and avoids nested spinning.

## 3. Joint-space motion (GotoJS) must be non-blocking

For the arm joint motion service (`/mars/arm/goto_js`):

- Create the `GotoJS` client once in `ManipulationInterface.__init__`.
- Use `client.service_is_ready()` as a **non-blocking** readiness check.
- Use `client.call_async(request)` in a **fire-and-forget** style.
- Do **not** call `wait_for_service()` or `spin_until_future_complete()` from inside primitive execution.

At this level, `move_to_joint_positions` should:

- Return `True` as soon as the request has been dispatched (or `False` if the client/service is clearly unavailable).
- Leave the lower-level GotoJS server responsible for handling motion timing and detailed errors.

## 4. "Primitive succeeded" vs. "motion finished"

With the non-blocking pattern:

- `move_arm_to_pose` reports **success** once:
  - IK has produced a valid joint configuration, and
  - The GotoJS request has been sent successfully.
- It does **not** wait for the physical motion to complete.

If a primitive **must** block until motion completion, prefer:

- A higher-level action or topic signaling motion completion, or
- A separate node that owns the blocking waits, so the brain/action server executor stays responsive.

## 5. General guideline

When in doubt: treat the `PrimitiveExecutionActionServer` execute callback as a place to **orchestrate** short, non-blocking operations, not a place to perform long or nested spins. Long-running work should either:

- Be delegated to other actions/services and handled asynchronously, or
- Run in separate nodes/threads that do their own spinning.
