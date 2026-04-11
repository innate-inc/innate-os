# Contributing

## Building from Source

### Dependencies

All dependencies are managed through config files in `ros2_ws/`:

| File | Description | Usage |
|------|-------------|-------|
| `apt-dependencies.txt` | System & ROS2 apt packages | `xargs sudo apt-get install -y < apt-dependencies.txt` |
| `pip-requirements.txt` | Python packages | `pip3 install -r pip-requirements.txt` |
| `src/dependencies.repos` | External ROS2 repositories | `vcs import src < src/dependencies.repos` |

### Adding Dependencies

- **APT packages**: Add to `ros2_ws/apt-dependencies.{common/hardware}.txt`
- **Python packages**: Add to `ros2_ws/pip-requirements.txt`

These files are used automatically by the local build and CI/CD pipeline.

## Releases

Releases are automatically built via GitHub Actions when a version tag is pushed:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Each release includes:
- `innate-os-{version}.tar.gz` - Full release with pre-built artifacts
- `innate-os-{version}-source.tar.gz` - Source code only
