# Simulation Mode

Use the local Innate CLI for simulator development:

```bash
./innate sim setup
./innate sim up
```

The launcher owns the Docker container, ROS workspace validation, simulator
backend, frontend build, optional cloud-agent mode, and startup dashboard.
Avoid running the Docker Compose commands directly during normal development;
that bypasses the launcher's caching and readiness checks.

## Common Commands

```bash
./innate sim up --once        # start, validate readiness, print one status snapshot
./innate sim up --vis         # start with the native Genesis viewer
./innate sim status           # show current runtime status
./innate sim logs startup     # show captured startup logs
./innate sim logs brain       # show recent brain-client logs
./innate sim down             # stop the simulator runtime
```

## Docker Modes

The default local Docker image is built in simulation mode. Hardware-specific
packages are only installed for hardware images.

```bash
docker build --build-arg MODE=hardware -t innate-os .
```

Use direct Docker commands only for image development or hardware packaging.
For day-to-day simulator work, use `./innate sim ...`.

## Related Documentation

- [dev/launcher/README.md](dev/launcher/README.md) - Local simulator CLI
- [ros2_ws/DEPENDENCIES_GUIDE.md](ros2_ws/DEPENDENCIES_GUIDE.md) - ROS system dependencies
- [SYSTEM_SETUP.md](SYSTEM_SETUP.md) - Full robot system setup
