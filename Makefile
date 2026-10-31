.PHONY: lint type test smoke up down
lint:
	uv run ruff check . && uv run ruff format --check .
type:
	uv run mypy
test:
	uv run pytest
smoke:
	uv run pytest -m smoke tests/smoke
REVISION ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
up:
	./scripts/init-secrets.sh && ./scripts/check-data.sh && TRADING_RELEASE_REVISION=$(REVISION) docker compose up -d --build
down:
	docker compose down
