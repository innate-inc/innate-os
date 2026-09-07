# workspace

Where agents and skills live on disk. Every skills directory is an ordinary
Python package — imported, not scanned.

```
innate_agents/   Shipped agents. Tracked in git, updated by `git pull`.
custom_agents/   Your agents. Gitignored, stays on your machine.
innate_skills/   Shipped skills. Tracked in git.
custom_skills/   Your skills (code and physical). Gitignored.
<anything>/      A skill package: someone's skills + helpers, installed by dropping the folder in.
```

A skill is a class: define a `Skill` subclass anywhere in a package and the
robot knows it — defining it is the registration (like a PyTorch `nn.Module`).
The class name is the identity (`class PickSocks` → `pick_socks`), so files
organize however you like: several skills in one file, a skill split across a
subpackage with relative imports, helpers next to it. A `.py` that defines no
`Skill` is just a module you import. Physical skills stay data: a directory
with `metadata.json`. The catalog drops a generated `__init__.py` ref next to
that metadata, so `from innate_skills.wave import Wave` imports a typed handle
to the recording (the same class as `from physical_skills import Wave`).

Everything auto-loads on brain_client start, edits hot-reload on save, and a
module that fails to import shows up in the web app marked broken with its
error (and clears when you fix it) instead of vanishing.

Skill IDs are namespaced by package: `innate-os/<name>` for shipped,
`local/<name>` for yours, `<package>/<name>` for dropped-in packs. Packages
import each other by bare name (`from innate_skills import arm_utils`). See
the [skills guide](../docs/skills/README.md) for authoring and running skills.

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
