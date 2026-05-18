# Innate OS convenience targets.
#
# The supported local workflow is `./innate sim ...`; keep make as a thin
# compatibility layer so it cannot bypass launcher caching or startup checks.

.PHONY: help setup sim up once down status logs build clean test

help:
	@echo "Innate OS - use ./innate sim ..."
	@echo "  make setup   -> ./innate sim setup"
	@echo "  make sim     -> ./innate sim up"
	@echo "  make up      -> ./innate sim up"
	@echo "  make once    -> ./innate sim up --once"
	@echo "  make down    -> ./innate sim down"
	@echo "  make status  -> ./innate sim status"
	@echo "  make logs    -> ./innate sim logs startup"
	@echo "  make test    -> Docker integration test"

setup:
	./innate sim setup

sim up:
	./innate sim up

once:
	./innate sim up --once

down:
	./innate sim down

status:
	./innate sim status

logs:
	./innate sim logs startup

build: once

clean: down

test:
	@echo "Running integration tests..."
	docker build --progress=plain -t innate-os-test:latest -f Dockerfile.test . 2>&1
	INNATE_TEST_IMAGE=innate-os-test:latest docker compose -f docker-compose.test.yml up --abort-on-container-exit --exit-code-from integration-test
	@docker compose -f docker-compose.test.yml down
