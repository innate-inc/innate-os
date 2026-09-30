# workspace

Where your skills and agents live. This is the whole extension surface: write a
class, drop it in, and the robot can do a new thing.

## Your first skill

Put this in `custom_skills/battery_level.py`:

```python
from innate import Battery, Skill, SkillReturn


class BatteryLevel(Skill):
    """Report how much charge the robot has left. Use this when the user asks
    about the battery, or before starting anything long."""

    battery: Battery

    def execute(self) -> SkillReturn:
        percent = round(self.battery.percentage * 100)
        if self.battery.charging:
            return f"Battery is at {percent}% and charging."
        return f"Battery is at {percent}%."
```

Save it. The brain reloads on save, the web app lists it, and an agent can call
it as `local/battery_level`.

That file is the entire contract:

- **The class is the skill.** Defining a `Skill` subclass *is* the
  registration, and the class name is the identity: `BatteryLevel` →
  `battery_level`. There is nothing to register, import or list anywhere else.
- **The docstring is what the agent reads** when it decides whether to call
  you. Write it for the model: what this does, and when to reach for it.
- **A bare annotation asks for something.** `battery: Battery` means the
  framework hands you a battery reading, and it is guaranteed to be there
  inside `execute()` — no `None` checks. The same rule covers the cameras, the
  arm, the base, the map and spatial memory. Annotate what you read.

## Where to go next

Three shipped skills worth copying, in order of size:

| File | Lines | Shows |
|---|---|---|
| `innate_skills/arm/arm_rest_position.py` | 18 | The smallest useful shape: one feed, arguments with defaults, a message back |
| `innate_skills/search_memory.py` | 28 | Waiting on a slow result, failing a run, returning a structured payload with an image |
| `innate_skills/turn_in_place.py` | 55 | A cancellable loop, a deadline, and a typed payload for callers that chain |

The full reference lives in the `innate` namespace itself — every feed you can
annotate, what `execute()` may return, the camera overlay API, and the
cancellation rules. Read it where `brain_client` is importable (on the robot, or
`./innate-sim sh`):

```bash
python3 -c "import innate; help(innate)"
```

Guides and the wider API are on [docs.innate.bot](https://docs.innate.bot):
[skills](https://docs.innate.bot/software/skills),
[agents](https://docs.innate.bot/software/agents), and
[inputs](https://docs.innate.bot/software/inputs).

Agents are authored the same way, from the same namespace — `from innate import
Agent`, with `SkillRef` and `InputRef` typing what `get_skills()` and
`get_inputs()` may list. `innate_agents/basic_agent.py` is the short one.

## How loading works

Every skills directory is an ordinary Python package — imported, not scanned.

```
innate_agents/   Shipped agents. Tracked in git, updated by `git pull`.
custom_agents/   Your agents. Gitignored, stays on your machine.
innate_skills/   Shipped skills. Tracked in git.
custom_skills/   Your skills (code and physical). Gitignored.
<anything>/      A skill package: someone's skills + helpers, installed by dropping the folder in.
```

Because defining the class is the registration (like a PyTorch `nn.Module`),
files organize however you like: several skills in one file, a skill split
across a subpackage with relative imports, helpers next to it. A `.py` that
defines no `Skill` is just a module you import. Physical skills stay data: a
directory with `metadata.json`. The catalog drops a generated `__init__.py` ref
next to that metadata, so `from innate_skills.wave import Wave` imports a typed
handle to the recording (the same class as `from physical_skills import Wave`).

Everything auto-loads on brain_client start, edits hot-reload on save, and a
module that fails to import shows up in the web app marked broken with its
error (and clears when you fix it) instead of vanishing.

Skill IDs are namespaced by package: `innate-os/<name>` for shipped,
`local/<name>` for yours, `<package>/<name>` for dropped-in packs. Packages
import each other by bare name (`from innate_skills import arm_utils`).

A pack that lives elsewhere on disk (a team checkout, a mounted volume) is
symlinked in rather than copied — it then behaves exactly like a dropped-in
folder: discovered at boot, hot-reloaded on edit, ids namespaced by the link
name (`team_skills/<name>`):

```bash
ln -s /opt/team/skills ~/innate-os/workspace/team_skills
```

(This replaces the 0.6.x `extra_skill_dirs` / `extra_agent_dirs` setting. In
the sim/Docker setup the link target must also be mounted into the container,
or it dangles there and the pack is skipped.)
