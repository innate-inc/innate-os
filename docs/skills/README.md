# Skills

[Back to Innate OS](../../README.md#skills)

Skills are the core unit of action on Innate robots.

A skill can be digital, like calling a tool, a service or another agent; or physical, like navigating, waving, grasping, recording a demonstration, or executing a learned manipulation policy.

- **Execute manually** — Run skills from the CLI, web app, or mobile apps.
- **Run autonomously** — Let agents select and interrupt skills as the world changes.

## Running a skill

Ask MARS to find and pick up an object from the floor. On a configured robot with Innate vision access, place a sock in view and run:

```bash
innate skill type innate-os/pick_any_object
innate skill run innate-os/pick_any_object @prompt="the white sock"
```

MARS looks for the object, approaches it, grasps it, and checks whether the pick succeeded. Change `@prompt` to describe another object. [See the picking skill](../../workspace/innate_skills/pick_any_object.py).

## Write a skill

A skill is a Python class. You can control the robot's interfaces directly or build on an existing skill. Here is a custom skill that reuses `PickAnyObject` to pick up a sock.

**Create** `workspace/custom_skills/pick_up_sock.py`:

```python
from innate_skills.pick_any_object import PickAnyObject

from innate import Skill, SkillReturn


class PickUpSock(Skill):
    """Find a white sock on the floor and pick it up."""

    pick: PickAnyObject

    def execute(self) -> SkillReturn:
        return self.pick(prompt="the white sock")
```

**Save and run it** on the robot:

```bash
innate skill run local/pick_up_sock
```

The runtime discovers the class and hot-reloads edits on save. The `pick` annotation declares the skill to reuse; the runtime wires it up. This custom skill runs the same picking behavior and returns its result. Failures and cancellation propagate to the caller.

Change the prompt in your file, save, and run it again to pick up a different object. You can also trigger your skill from the web or mobile app, or give it to an [agent](../../README.md#agents).

## Organizing skills and sharing helpers

- **Built-in skills** — Located in `workspace/innate_skills/`, with IDs such as `innate-os/pick_any_object`.
- **Your custom skills** — Stored in `workspace/custom_skills/`, with IDs such as `local/pick_up_sock`. Gitignored and yours to play with.
- **Skill packs** — Any other folder dropped into `workspace/` loads as its own package (IDs `<folder>/<name>`). A pack that lives elsewhere on disk is symlinked in (`ln -s /opt/team/skills workspace/team_skills`) and works the same, hot reload included. In the simulator, the link target must also be mounted into the container.

Helpers work like normal Python: any `.py` in your skills folder that doesn't define a `Skill` is just a module — `import` it, use relative imports inside subfolders, share across packages by bare name (`from innate_skills import arm_utils`). Device helpers are methods on the interfaces (`self.manipulation.move_to(...)`, `self.mobility.rotate_by(...)`); camera math and Gemini live under `innate` (`from innate import geometry, vision, gemini`).

When writing your own motion loops, use `self.sleep(seconds)` so Stop can interrupt a pause. The framework handles cancellation and brakes the base; put your own cleanup in `try/finally`. A committed grasp may finish its lift and carry motion before returning, so cancelling a pick does not drop the object. Return a message or `SkillOutput` for success, or call `self.fail(message)` to fail the run.

See [the workspace guide](../../workspace/README.md) for package layout and hot reload.

## Trained skills

Some physical skills can be learned from demonstrations.

- Record episodes from the phone app or web app.
- Train a policy with one of the models available on Innate Cloud or locally.
- Deploy the trained model as a skill.

Start here: [Training overview](https://docs.innate.bot/training/overview). To ship a trained model back to the robot, see [Deploy a trained skill](https://docs.innate.bot/training/deploy-trained-skill).

## Replay a recorded motion

A replay skill plays back a recorded motion file. Save the following as `workspace/custom_skills/greet/metadata.json`, replacing the example recording URL and start/end poses with your own:

```json
{
    "name": "greet",
    "type": "replay",
    "guidelines": "Greet the user with a friendly arm wave.",
    "inputs": {},
    "wheeled": false,
    "downloads": {
        "episode_0.h5": "https://your-cdn.com/greet/episode_0.h5"
    },
    "execution": {
        "model_type": "replay",
        "replay_file": "episode_0.h5",
        "replay_frequency": 50.0,
        "start_pose": [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.0],
        "end_pose": [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.0]
    }
}
```

## Requirements and troubleshooting

The picking example uses the robot cameras, base, arm, and Innate vision service. The robot needs a configured `INNATE_SERVICE_KEY` with Gemini access; a missing key is reported as a skill failure. See [configuration](../PARAMETERS.md).

- Use `innate skill list` to check discovery and `innate skill type <skill-id>` to inspect inputs.
- If a module fails to import, the web app marks it as broken and shows the error. Fix the file and save again.
- A missed grasp is a failure, not a successful pickup. Composed skills propagate that failure to their caller.
- Use the [simulator](../../sim/README.md) to develop skills before trying them on a physical robot.
