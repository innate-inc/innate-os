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

The offset scales with `1/kp`, so it re-solves itself whenever the gains change.

### It is off by default, because it loses to the integral gains

Measured on the real arm over nine held poses, identical in each run
(`/debug/arm-tracking.html`), RMS gripper error and mean droop:

| configuration | gripper error | droop | settles under 10 mm |
|---|---|---|---|
| `ki` on, no compensation (the original) | **6.5 mm** | **−0.9 mm** | 9 of 9 dwells |
| compensation on, `ki` forced to 0 | 16.5 mm | −6.1 mm | 1 of 9 |
| neither | 44.1 mm | −32.9 mm | 1 of 9 |

Both mechanisms are worth a lot against nothing at all. But the integral term is twice as
good as compensation, and it wins the transient too — 8.3 mm at 0.1 s after the command
settles, against 15.4 mm, and it keeps improving where compensation is flat forever.

The reason is that **gravity is not what dominates this arm's steady-state error.** Each joint
has a friction band 3–6° wide at teleop gains, wider than the sag itself: where a joint lands
depends on which way it last moved. Adding the approach direction to a fit of error against
gravity torque lifts R² from 0.22–0.35 to 0.76–0.95. An integral term erases that, because it
winds up until the joint breaks free; a feedforward cannot see friction at all, only gravity.
Joint 1 is the clean proof — it carries no gravity torque whatsoever, yet sits 1.3° off its
target without `ki` and 0.28° off with it.

So compensation is now purely additive: it changes goal positions and nothing else. `ki`,
`gains_far` and gain scheduling are whatever `arm_config.yaml` says, compensated or not.

**What it is still good for.** The gravity model itself is exact (see above) and independent of
all this: it is what the sim compensates against, and it is the thing to reach for if the arm
ever gets a current-control mode, a payload estimate, or a torque-limit check. And if the
friction is ever reduced — a different geartrain, a stiffer cable run — the balance above could
change, so the switch is left in place.

The fitted trims from that session, if compensation is ever turned back on: `joint_2` 1.25,
`joint_3` 1.14, `joint_4` 1.01 N·m. They come from the direction-corrected slopes, so they are
the gravity part alone, and they say the datasheet stall torques were within about 25%.

### Calibrating it on the robot

`full_pwm_torque_nm` per joint is the only knob. It starts at the datasheet stall torque, and
trimming it trims that joint's compensation — it absorbs voltage sag, gear efficiency and
stiction as well as its nominal meaning.

1. Turn it on (`gravity_compensation.enabled`), then check the boot lines: each compensated
   joint logs its full-PWM torque, its `kp`, and the resulting stiffness in N·m/rad.
2. Watch the offsets the model is actually applying: raise the node's log level to debug and
   read the throttled `GravComp (deg)` line, one entry per second.
3. Measure it: open `/debug/arm-tracking.html` on the robot and hit **Run sweep**. It drives
   the arm to its zero pose and through eight more, pausing at each, then repeats the
   *identical* poses after you flip compensation — so the two runs differ in one thing. The
   poses load the shoulder, elbow and wrist over the widest range the joint limits allow
   (0.50 / 0.25 / 0.06 N·m, each swinging through zero) while keeping the gripper clear of
   the floor; offsets around a parked, folded arm span barely a tenth of that and never
   change sign, which is not enough for a fit to bite on. It
   pairs `/mars/arm/command_state` (what the arm was asked to hold) with `/mars/arm/state`
   (what the encoders read) and reports RMS error per joint plus the distance the gripper
   actually missed by, in mm.

   Read the **signed mean, held** column — that is the number to trim against. Each pose is
   approached from both sides, so the friction band cancels there and what is left is what
   gravity is still getting wrong. The RMS columns keep the band in, because it is part of how
   far the arm really lands. Sag is a steady-state error, so it only exists while the arm is
   holding; hand teleop never stops moving and measures tracking lag instead — a 35 s hand
   session yielded 14 usable samples, a sweep yields ~1700 across 18 dwells, and takes
   about 100 s.
4. A joint that still sags wants a **smaller** `full_pwm_torque_nm`; one that overshoots its
   target wants a **larger** one. The relationship is linear, so one correction converges.

   Calibrate against a **live** toggle, not a restart. Restarting with `enabled: false`
   restores the integral gains, and an integral term erases steady-state error by itself —
   so that comparison answers "is the new config better than the old one", not "is the model
   right". Toggling live holds the gains still and leaves the offset as the only variable.
5. Whatever is left after that is deflection *past* the encoder — gear play and link flex,
   which no goal offset can reach, because the servo cannot see it. That residual is the
   direct measurement the sim's `STRUCT_STIFFNESS` / `ARM_BACKLASH_RAD` are still guessing at
   (`mars_sim_driver/world.py`).

### Turning it on and off against a held pose

`gravity_compensation.enabled` is live, and `/debug/arm-tracking.html` has a button for it.
By hand, put the arm somewhere loaded, then flip it:

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
