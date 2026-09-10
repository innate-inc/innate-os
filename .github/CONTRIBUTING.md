# Contributing

## Building from Source

### Dependencies

All dependencies are managed through config files in `ros2_ws/`:

| File | Description | Usage |
|------|-------------|-------|
| `apt-dependencies.common.txt` | System & ROS2 apt packages shared by sim + robot | `xargs sudo apt-get install -y < apt-dependencies.common.txt` |
| `apt-dependencies.sim.txt` | Simulation-only overlay | install on top of common |
| `apt-dependencies.hardware.txt` | Physical-robot / Jetson-only overlay | install on top of common |
| `pip-requirements.txt` | Python packages | `pip3 install -r pip-requirements.txt` |
| `src/dependencies.repos` | External ROS2 repositories | `vcs import src < src/dependencies.repos` |

### Adding Dependencies

- **APT packages**: Add to `ros2_ws/apt-dependencies.{common,sim,hardware}.txt` — `common` if both sim and robot need it, otherwise the mode-specific overlay
- **Python packages**: Add to `ros2_ws/pip-requirements.txt`

These files are used automatically by the local build and CI/CD pipeline.

## Code Style

Formatting and linting (ruff for Python, clang-format for C/C++) are enforced in
CI via [`.github/workflows/format.yml`](workflows/format.yml), so an
unformatted PR will fail the **Format Check** job. To catch issues locally
*before* pushing, install the pre-commit hook so it runs on every `git commit`:

```bash
pip install pre-commit
pre-commit install -c .config/pre-commit-config.yaml
```

The hook versions are pinned in
[`.config/pre-commit-config.yaml`](../.config/pre-commit-config.yaml), so local
and CI results are identical. The `-c` flag is required because the config lives
under `.config/` rather than the repo root. To run the checks manually across
the whole repo:

```bash
pre-commit run --all-files -c .config/pre-commit-config.yaml
```
