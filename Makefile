# Innate OS Makefile
#
# Usage:
#   make sim        - Start simulation (builds if needed, launches ROS nodes, attaches to tmux)
#   make build      - Build Docker image
#   make up         - Start containers
#   make down       - Stop containers
#   make shell      - Open a shell in the container
#   make logs       - Show container logs
#   make clean      - Stop containers and remove volumes
#   make test       - Run integration tests in Docker
#   make install-hooks - Install repository git hooks
#

COMPOSE_FILE := docker-compose.dev.yml
CONTAINER := innate
TMUX_SESSION := mars

.PHONY: sim build up down shell logs clean help test install-hooks

# Default target
help:
	@echo "Innate OS - Available commands:"
	@echo "  make sim     - Start simulation (one-liner: build + up + launch + attach)"
	@echo "  make build   - Clear build volumes + rebuild Docker image"
	@echo "  make up      - Start containers"
	@echo "  make down    - Stop containers"
	@echo "  make shell   - Open a shell in the container"
	@echo "  make logs    - Show container logs"
	@echo "  make clean   - Stop containers and remove volumes"
	@echo "  make test    - Run integration tests in Docker"
	@echo "  make install-hooks - Install repository git hooks"

# One-liner to start simulation
sim: up
	@if docker compose -f $(COMPOSE_FILE) exec -T $(CONTAINER) tmux has-session -t $(TMUX_SESSION) 2>/dev/null; then \
		echo "Attaching to existing simulation session..."; \
		docker compose -f $(COMPOSE_FILE) exec $(CONTAINER) tmux attach -t $(TMUX_SESSION); \
	else \
		echo "Launching simulation nodes..."; \
		docker compose -f $(COMPOSE_FILE) exec $(CONTAINER) zsh -lc './scripts/launch_sim_in_tmux.zsh && tmux attach -t $(TMUX_SESSION)'; \
	fi

# Build Docker image (clears build volumes to ensure fresh build)
build: down
	@echo "Removing build volumes..."
	docker volume rm innate-os_ros2_ws_build innate-os_ros2_ws_install innate-os_ros2_ws_log 2>/dev/null || true
	docker compose -f $(COMPOSE_FILE) build

# Start containers (build if needed)
up:
	@if ! docker compose -f $(COMPOSE_FILE) ps --status running 2>/dev/null | grep -q "$(CONTAINER)"; then \
		echo "Starting containers..."; \
		docker compose -f $(COMPOSE_FILE) up -d; \
		sleep 2; \
	fi

# Stop containers
down:
	docker compose -f $(COMPOSE_FILE) down

# Open a shell in the container
shell:
	docker compose -f $(COMPOSE_FILE) exec $(CONTAINER) zsh -l

# Show container logs
logs:
	docker compose -f $(COMPOSE_FILE) logs -f

# Stop containers and remove volumes
clean:
	docker compose -f $(COMPOSE_FILE) down -v

# Run integration tests in Docker
test:
	@echo "Running integration tests..."
	docker build --progress=plain -t innate-os-test:latest -f Dockerfile.test . 2>&1
	INNATE_TEST_IMAGE=innate-os-test:latest docker compose -f docker-compose.test.yml up --abort-on-container-exit --exit-code-from integration-test
	@docker compose -f docker-compose.test.yml down

# Install repository git hooks
install-hooks:
	@chmod +x .githooks/pre-commit
	git config core.hooksPath .githooks
	@echo "Git hooks installed from .githooks"
