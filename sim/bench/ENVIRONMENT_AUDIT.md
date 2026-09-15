# Environment solvability audit — 14 September 2026

**Update, 15 September:** This is the historical audit. The six-world refresh
adapts the 15 raised-surface pickup tasks below to floor pickup and visible
floor destinations, adds height bounds and a settling interval to deliveries,
and includes Stocktake’s return instruction. See [the refresh notes](../bundles/REFRESH.md).
Household’s missing recipient and unstable fallen-person setup are outside
that refresh and remain open. Original scores do not describe the new layouts.

The current suite contains tasks that are unsupported by the live skills, tasks whose instructions omit information needed to satisfy the judge, and a placement check that accepts an object intersecting furniture. A better language model alone will not resolve these mismatches.

This audit covers the working tree in `innate-os-bench`, including the recent pantry assets and sorted counting layout. It adds findings and reproduction evidence; it does not change the tasks or weaken their goals.

## What was checked

- Loaded all **45 challenge setups across eight worlds** through their environment packs, advanced physics for 1.5 seconds, and recorded object positions, support heights, drift, and deep contacts.
- Ran every available scripted oracle: **42/42 passed**. The other three challenges require skill-completion events and have no oracle. **20 of the passing plans use teleport-assisted placement.** These results certify neither visual interpretation nor execution by the live skills.
- Reproduced the counter placement, missing recipient, and hidden Stocktake goal using the real simulator and challenge engine.
- Inspected the live Demo Agent's skill roster and the implementation of its pick/release skills. Verified the live navigation map is `pantry.yaml`.

No hosted model evaluations or paid inference were run. Random baselines were not rerun: the oracle-only command correctly reports the validity gate as incomplete. This is a solvability audit, not a new benchmark score.

## Confirmed findings

### 1. Fifteen tasks require a pickup the live skill does not support

**Priority: high. Scope: live agent capability mismatch.**

`pick_any_object` explicitly implements floor pickup: it projects detections onto the floor, approaches using `FloorApproach`, descends to a floor grasp, and verifies against the floor. It has no surface-height input or raised-surface grasp path. The selected Demo Agent has this skill, `open_gripper`, and `drop_in_box`; a separate shelf-pick skill is absent.

The physics survey found required pickup objects resting on surfaces **13–24 cm above the floor** in these 15 tasks:

| World | Affected challenges |
|---|---|
| Blaze | `blaze_l1`, `blaze_l2`, `blaze_l3`, `blaze_l4` |
| Counter | `counter_within_reach`, `counter_serve_the_red`, `counter_change_of_mind`, `counter_not_for_you`, `counter_which_one`, `counter_second_order`, `counter_three_orders`, `counter_cafe_shift` |
| Pantry | `pantry_restock`, `pantry_misfiled`, `pantry_stocktake` |

For example, Pantry's delivery carton settles on a 24 cm bench; its misplaced jar sits on a 13 cm shelf. Neither is a floor-pick target. The capability gate returns no blocking reason for these tasks once a proxy key is configured: it checks backend availability and an aperture manifest, not supported pickup surfaces.

This is **unsupported by the intended live action contract**, not a proof that the robot's mechanics could never lift such an object. Accidental pushing or a model misclassifying a shelf as floor would not establish a working shelf-pick capability. This limitation was already noted in the floor-control challenge documentation; the current audit confirms it remains in the running configuration.

**Correction:** mark these tasks unsupported for this skill profile until a raised-surface pickup path is implemented and demonstrated. Keep separate floor controls for instruction following. An abstract `pick`/teleport oracle must not certify live manipulation.

Evidence: [floor pickup implementation](../../workspace/innate_skills/pick_any_object.py), [capability gate](../../sim/bench/capabilities.py).

### 2. “Mug from the kitchen” asks for a recipient who does not exist

**Priority: high. Scope: missing observable destination.**

The brief asks the robot to bring a mug to “the person in the living room.” Starting the challenge resets the world and drops **only `household_mug_kitchen`**. The human and resident props remain parked outside the scene. The judge instead accepts a fixed circle at **(-1.3, 2.6), radius 0.7 m**, around a room marker.

A robot can search correctly and never find the requested person. The oracle passes because it is given the hidden delivery coordinates. Reproduced by starting the challenge and checking all active object poses: the mug is the sole object.

**Correction:** spawn a visible recipient and judge delivery relative to that person, or explicitly name the visible room marker in the brief. Merely widening the hidden circle does not supply the missing information.

Evidence: [brief, setup and destination](../../sim/bundles/household/challenges/42_fetch_from_kitchen.py).

### 3. The counter oracle passes with the jar embedded inside the counter

**Priority: high. Scope: invalid solvability witness / false positive.**

`counter_within_reach` accepts a jar anywhere in the counter rectangle with its origin above **0.10 m**, while the counter top is **0.24 m** high. The jar's default release height is **0.194 m**. Both the scripted oracle's `put` and the in-process agent's `place` use that default at the destination.

Reproduction: start the challenge, call the same `drop_prop_at` operation at **(0, 1.42)**, let physics run for a full second, then tick the real judge. The jar settles at **z = 0.19999 m**, with approximately **31.4 mm of penetration** into both the counter body and its top. The engine reports **passed, 2/2**. This is not just a transient airborne sample.

The floor-control variant shares the same weak counter-placement predicate, although it additionally requires a pickup completion. Thus an oracle pass here does not demonstrate a physically valid placement, and the in-process action cannot request an appropriate release height.

**Correction:** placement must use the destination surface height and object geometry. The goal must require a released, settled object supported on the intended surface, with no substantial penetration. Revalidate the floor/shelf pair after correcting the executor and predicate together; changing the threshold alone exposes the broken placement path without repairing it.

Evidence: [counter goal](../../sim/bundles/counter/challenges/13_within_reach.py), [oracle placement](../../sim/bench/planner_agent.py), [in-process placement](../../sim/bench/brain_agent.py).

### 4. Stocktake has a fifth instruction the robot never receives

**Priority: medium. Scope: hidden requirement / false negative.**

The brief asks for four things: report the jar count, report the shelf-carton count, shelve the delivery, and return the misplaced item. Its only reminder also names the delivery and misplaced item. The judge additionally requires returning to the door.

A controlled engine probe supplied both correct counts and placed both objects in their required bays. The result was **4/5, still running**, with only “Back at the door” incomplete. The human's challenge panel reveals that goal, but the robot receives the brief and cues rather than the panel's checklist. Following the spoken instructions alone is insufficient; returning to the door happens to satisfy an undisclosed condition.

**Correction:** add the return instruction to the brief, or remove that goal. Also remove stale comments describing a different five-goal sequence.

Evidence: [Stocktake brief and goals](../../sim/bundles/pantry/challenges/30_stocktake.py).

## Additional issues, not proofs of an impossible task

- **Household's fallen person spawns across an internal wall.** Its feet start at (-3.4, -0.5), while its 1.7 m body extends across the partition at y=0. After 1.5 seconds it has moved **1.343 m** to approximately (-2.709, 0.651). The existing comment claiming calm settling is not true for this checkout. The oracle nevertheless passed, so this is an unstable scene/realism defect rather than demonstrated impossibility. Place the entire body clear of the wall and validate the motion immediately after reset, not only the final resting pose.
- **Bridge failure is still a deployment hazard.** The earlier live ROS bridge exited with signal 11 and was manually restarted. Its launch definition has no automatic respawn. The challenge clock is independent of that process, and the live capability gate does not check end-to-end control/answer transport. A disconnected trial must be reported as blocked/interrupted rather than attributed to the agent. Auto-restart helps recovery but must not silently validate a trial whose instructions or answers were lost.
- **The suspected narrow-door blocker was not confirmed.** The published footprint is updated dynamically; the static YAML rectangle alone is insufficient evidence. The authored first Rounds doorway is 0.45 m wide, despite stale 0.35 m descriptions. All scripted navigation runs passed. No claim is made that every live Nav2 route has been exercised.

## Reproduction evidence

The audit produced local scene surveys, oracle episodes and targeted engine
reproductions; those workstation artifacts are not checked in. The findings
above describe the pre-refresh layouts. For the current scenes, regression
checks and reproduction commands, see [the refresh notes](../bundles/REFRESH.md).

The next validity check should execute through the same skills the evaluated agent receives, require healthy input/output transport, and verify physically supported outcomes. Otherwise it can continue to label these scenarios solvable while bypassing the very obstacle the live robot encounters.
