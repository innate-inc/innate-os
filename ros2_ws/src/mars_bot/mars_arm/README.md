# mars_arm

The arm + head node: seven Dynamixel servos on one serial bus, a 200 Hz control loop,
trajectory planning, and a KDL IK node (`ik.py`). This document covers only gravity
compensation, which is the one part of the node whose behaviour comes from physics rather
than from tuning.

## Gravity compensation

The servos have no torque control — they take a goal position and run their own PID.
A joint that must hold torque `τ` therefore *cannot* sit on its goal: the P term only
produces output from position error, so the joint settles exactly as far below its goal as
it needs to generate `τ`. That is the arm's sag, and it is not small: at the operating
gains the gripper hangs 5–12 mm below where it was told to be, and at the soft teleop gains
up to 46 mm.

Two things fix that, both exactly computable:

**What torque each joint must hold.** `gravity.cpp` reads the masses and centres of mass out
of `mars.urdf` and returns the static holding torque per joint. Links rigidly attached to a
moving link (the cameras, `ee_link`) are folded into it once at construction, so the runtime
cost is a handful of trig calls. The model agrees with MuJoCo's `qfrc_bias` on the same URDF
to 5e-13 N·m over 300 random poses — the sim and the robot are compensating against the same
number, not two approximations of it.

**How much goal offset that torque costs.** X-series position control drives PWM from
position error as `PWM = (kp / 128) · error[counts]`, and full-scale PWM (885) commands the
motor's full-PWM torque. Invert it (`gravityGoalOffsetRad` in `arm_types.hpp`) and you get
the offset to *add* to the goal so the link lands on the target instead of below it.

The offset scales with `1/kp`, so it re-solves itself whenever the gains change. That is what
makes the rest of the anti-sag machinery unnecessary:

- **No integral gain.** `ki` existed to creep a loaded joint back onto its target. With
  `gravity_compensation.enabled` the node forces `ki` to 0 everywhere.
- **No gain scheduling.** `gains_far` stiffened an extended arm out of its own sag; the node
  now sets `far := near`, which makes the scheduler's interpolation a no-op.
- **Soft gains stop sagging.** The teleop gains were the worst case (up to 46 mm); they are
  now compensated like any other, which is why sag no longer argues with compliance.

### Calibrating it on the robot

`full_pwm_torque_nm` per joint is the only knob. It starts at the datasheet stall torque, and
trimming it trims that joint's compensation — it absorbs voltage sag, gear efficiency and
stiction as well as its nominal meaning.

1. `ros2 launch mars_arm arm.launch.py`, and check the boot lines: each compensated joint
   logs its full-PWM torque, its `kp`, and the resulting stiffness in N·m/rad.
2. Watch the offsets the model is actually applying: raise the node's log level to debug and
   read the throttled `GravComp (deg)` line, one entry per second.
3. Command a pose, let it settle, and compare `/mars/arm/command_state` (what the arm was
   asked to hold) against `/mars/arm/state` (what the encoders read). Residual error per
   joint is what compensation missed.
4. A joint that still sags wants a **smaller** `full_pwm_torque_nm`; one that overshoots its
   target wants a **larger** one. The relationship is linear, so one correction converges.
5. Whatever is left after that is deflection *past* the encoder — gear play and link flex,
   which no goal offset can reach, because the servo cannot see it. That residual is the
   direct measurement the sim's `STRUCT_STIFFNESS` / `ARM_BACKLASH_RAD` are still guessing at
   (`mars_sim_driver/world.py`).

### Turning it on and off against a held pose

`gravity_compensation.enabled` is live. Put the arm somewhere loaded, then flip it:

```
ros2 param set /mars_arm gravity_compensation.enabled false
```

— or use Settings → Safety & hardware → Arm, or edit `arm_config.yaml` with
`pid_hot_reload.py` running. The joints visibly settle when it goes off and rise when it
comes back, which is the whole measurement.

That switch moves **only the offsets**. The gains compensation replaced — `ki` forced to 0,
`gains_far := gains_near` — are written once at launch and stay put, so a live "off" is
"these gains, no compensation", not the pre-compensation tuning. For that, set
`enabled: false` in `arm_config.yaml` and restart: it restores scheduled gains, integral
terms and all.

The model itself loads whenever `gravity_compensation.urdf_path` is set, so the switch works
in both directions regardless of which way the node booted.

### Where the URDF numbers come from

The CAD export left every link's centre of mass at its link frame origin, which sits *on*
that link's own joint axis: a gravity model built from it reads exactly zero torque at the
head and badly under-reads the shoulder and elbow. `mars_description/tools/mesh_com.py`
replaces those origins with each visual mesh's volume centroid (uniform density within a
link). Re-run it with `--write` if the meshes change.

The masses themselves are unmeasured estimates, and uniform density is wrong for a link whose
mass is mostly one servo — which is precisely what the `full_pwm_torque_nm` trim absorbs.

### What it does not do

- **Payload.** The model knows the arm, not what the gripper is holding, so a carried object
  is uncompensated. Feeding a payload mass in at pick time is the obvious next step.
- **Base tilt.** Gravity is assumed to be −z in `base_link`. On a ramp the compensation is
  off by the tilt.
- **The gripper.** `joint_6` runs current-capped (mode 5), so there is no position stiffness
  to invert; it has no `full_pwm_torque_nm` and is left alone.
- **Dynamics.** Static holding torque only. Acceleration terms are the feedforward gains'
  job (`ff1`/`ff2`).
